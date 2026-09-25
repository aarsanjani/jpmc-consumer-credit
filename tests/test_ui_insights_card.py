import json

import pytest

from backend.provenance import NOT_RECORDED
from backend.ui_insights import (
    build_card_summary,
    card_product,
    customer_name,
    shape_accounts,
    shape_cards,
    summarize_cards,
    token_is_live,
)

ACCOUNTS = [
    {"account_id": "ACCT000003", "product_name": "Sapphire Preferred", "account_type": "CREDIT_CARD",
     "regulation_path": "REG_Z", "status": "OPEN", "current_balance": 912.4},
    {"account_id": "ACCT000001", "product_name": "Total Checking", "account_type": "CHECKING",
     "regulation_path": "REG_E", "status": "OPEN"},
]

NESTED_CARD_ROWS = [
    {"card_id": "CARD00002", "account_id": "ACCT000003", "last_four": "0513", "network": "VISA",
     "card_type": "PHYSICAL", "status": "FROZEN", "freeze_reason": "SUSPECTED_FRAUD_TRAVEL",
     "pan_full": "4532011234560513", "pan_masked": "453201******0513",
     "wallet_tokens": [
         {"token_id": "TOK00001", "wallet_provider": "APPLE_PAY", "device_name": "iPhone 16 Pro",
          "token_status": "REVOKED", "pan_masked": "453201******0513"},
         {"token_id": "TOK00002", "wallet_provider": "APPLE_PAY", "device_name": "Apple Watch",
          "token_status": "ACTIVE"},
     ]},
    {"card_id": "CARD00004", "account_id": "ACCT000001", "last_four": "6420", "network": "VISA",
     "card_type": "VIRTUAL", "status": "ACTIVE", "wallet_tokens": []},
]

FLAT_CARD_ROWS = [
    {"card_id": "CARD00002", "account_id": "ACCT000003", "last_four": "0513", "card_status": "FROZEN",
     "token_id": "TOK00001", "token_status": "REVOKED", "wallet_provider": "APPLE_PAY"},
    {"card_id": "CARD00002", "account_id": "ACCT000003", "last_four": "0513", "card_status": "FROZEN",
     "token_id": "TOK00002", "token_status": "ACTIVE", "wallet_provider": "APPLE_PAY"},
    {"card_id": "CARD00004", "account_id": "ACCT000001", "last_four": "6420", "card_status": "ACTIVE",
     "token_id": None},
]


def _golden(rows):
    return lambda customer_id: {"query_id": "xx", "rows": rows, "row_count": len(rows)}


def _raises(customer_id):
    raise RuntimeError("BigQuery down")


@pytest.mark.parametrize(("account", "expected"), [
    ({"account_type": "CREDIT_CARD"}, "credit"),
    ({"account_type": "CHECKING"}, "debit"),
    ({"account_type": "LOAN", "regulation_path": "REG_Z"}, "credit"),
    ({"account_type": "", "regulation_path": "REG_E"}, "debit"),
    ({"account_type": "LOAN"}, None),
    (None, None),
])
def test_card_product_comes_from_the_account(account, expected):
    assert card_product(account) == expected


def test_shape_accounts_keeps_only_whitelisted_fields_and_adds_product():
    shaped = shape_accounts(ACCOUNTS)

    assert shaped[0] == {"account_id": "ACCT000003", "product_name": "Sapphire Preferred",
                         "account_type": "CREDIT_CARD", "regulation_path": "REG_Z", "status": "OPEN",
                         "product": "credit"}


def test_shape_cards_nests_tokens_and_never_carries_pan_columns():
    cards = shape_cards(NESTED_CARD_ROWS, shape_accounts(ACCOUNTS))

    assert [(c["card_id"], c["product"], len(c["tokens"])) for c in cards] == [
        ("CARD00002", "credit", 2), ("CARD00004", "debit", 0)]
    assert "pan_" not in json.dumps(cards)


def test_shape_cards_groups_one_row_per_token():
    cards = shape_cards(FLAT_CARD_ROWS, shape_accounts(ACCOUNTS))

    assert [(c["card_id"], c["status"], [t["token_id"] for t in c["tokens"]]) for c in cards] == [
        ("CARD00002", "FROZEN", ["TOK00001", "TOK00002"]), ("CARD00004", "ACTIVE", [])]


def test_summarize_cards_counts_status_virtual_and_active_tokens():
    summary = summarize_cards(shape_cards(NESTED_CARD_ROWS, shape_accounts(ACCOUNTS)))

    assert summary == {"total_cards": 2, "by_status": {"FROZEN": 1, "ACTIVE": 1}, "virtual_cards": 1,
                       "active_tokens": 1, "products": ["credit", "debit"]}


@pytest.mark.parametrize(("rows", "expected"), [
    ([{"full_name": "Priya Vantalis"}], "Priya Vantalis"),
    ([{"first_name": "Priya", "last_name": "Vantalis"}], "Priya Vantalis"),
    ([{"email": "p@example.com"}], None),
    ([], None),
])
def test_customer_name_reads_the_first_name_column_present(rows, expected):
    assert customer_name(rows) == expected


def test_build_card_summary_tags_every_section_live():
    payload = build_card_summary("CUST00001", {
        "profile": _golden([{"full_name": "Priya Vantalis"}]),
        "accounts": _golden(ACCOUNTS),
        "cards": _golden(NESTED_CARD_ROWS),
    })

    assert payload["name"]["value"] == "Priya Vantalis"
    assert payload["cards"]["_provenance"]["tag"] == "LIVE_API"
    assert payload["summary"]["value"]["active_tokens"] == 1


def test_build_card_summary_reports_missing_wrappers_and_failures_as_nr():
    payload = build_card_summary("CUST00001", {"profile": None, "accounts": _raises,
                                               "cards": lambda cid: {"allowed": False, "error": "nope"}})

    assert payload["name"]["value"] == NOT_RECORDED
    assert "not available" in payload["name"]["_provenance"]["source"]
    assert "BigQuery down" in payload["accounts"]["_provenance"]["source"]
    assert "refused: nope" in payload["summary"]["_provenance"]["source"]


def test_shape_cards_falls_back_to_the_account_columns_on_the_card_row():
    rows = [{"card_id": "CARD00007", "account_id": "ACCT000004", "account_type": "CHECKING",
             "product_name": "Everyday Checking", "status": "ACTIVE", "wallet_tokens": None}]

    cards = shape_cards(rows, [])

    assert (cards[0]["product"], cards[0]["product_name"], cards[0]["tokens"]) == ("debit", "Everyday Checking", [])


@pytest.mark.parametrize(("token", "live"), [
    ({"token_status": "ACTIVE", "revoked_at": None}, True),
    ({"token_status": "ACTIVE", "revoked_at": "2026-09-14T14:22:00+00:00"}, False),
    ({"token_status": "SUSPENDED", "revoked_at": None}, False),
])
def test_token_is_live_matches_the_golden_query_rule(token, live):
    assert token_is_live(token) is live
