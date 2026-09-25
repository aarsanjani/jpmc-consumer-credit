"""Hermetic unit tests for customer_chat conversational router and streaming API."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend import customer_chat as cc


def test_greeting_intent_returns_fast_welcome(monkeypatch):
    monkeypatch.setattr(cc.adk, "customer_display_name", lambda cid: "Priya Vantalis")
    monkeypatch.setattr(
        cc.adk,
        "golden_13_customer_accounts_and_balances",
        lambda cid: {
            "rows": [
                {"account_id": "ACCT000001", "product_name": "Premier Checking", "available_balance": 37501.85},
                {"account_id": "ACCT000003", "product_name": "Horizon Sapphire Card", "available_balance": 21830.0},
            ]
        },
    )

    res = cc.evaluate_chat_turn("CUST00001", "hello")
    assert res["intent"] == "GREETING"
    assert "Priya Vantalis" in res["text"]
    assert "Premier Checking" in res["text"]
    assert "Horizon Sapphire Card" in res["text"]


def test_balances_intent(monkeypatch):
    monkeypatch.setattr(
        cc.adk,
        "golden_13_customer_accounts_and_balances",
        lambda cid: {
            "rows": [
                {"account_id": "ACCT000001", "product_name": "Premier Checking", "account_type": "CHECKING", "available_balance": 37501.85},
            ]
        },
    )
    res = cc.evaluate_chat_turn("CUST00001", "what is my checking balance?")
    assert res["intent"] == "BALANCES"
    assert "$37,501.85" in res["text"]


def test_cards_intent(monkeypatch):
    monkeypatch.setattr(
        cc.adk,
        "golden_14_customer_cards_and_wallets",
        lambda cid: {
            "rows": [
                {"card_id": "CARD00005", "last_four": "2758", "status": "ACTIVE", "product_name": "Virtual Card", "wallet_tokens": []},
                {"card_id": "CARD00002", "last_four": "0513", "status": "FROZEN", "freeze_reason": "SUSPECTED_FRAUD_TRAVEL", "product_name": "Sapphire", "wallet_tokens": []},
            ]
        },
    )
    res = cc.evaluate_chat_turn("CUST00001", "which cards are locked?")
    assert res["intent"] == "CARDS"
    assert "2758" in res["text"]
    assert "0513" in res["text"]
    assert "FROZEN" in res["text"]


def test_tracking_intent(monkeypatch):
    monkeypatch.setattr(
        cc.adk,
        "golden_17_card_replacement_tracking",
        lambda cid: {
            "rows": [
                {
                    "shipment_tracking_id": "TRK0000201",
                    "event_type": "DELIVERED",
                    "shipment_destination_type": "Temporary Hotel",
                    "shipment_destination_country": "PT",
                    "replacement_card_last_four": "9013",
                    "event_detail": {"carrier": "GlobalPost Express"},
                }
            ]
        },
    )
    res = cc.evaluate_chat_turn("CUST00001", "where is my replacement card shipping?")
    assert res["intent"] == "TRACKING"
    assert "TRK0000201" in res["text"]
    assert "GlobalPost Express" in res["text"]
    assert "9013" in res["text"]


def test_dispute_intent(monkeypatch):
    monkeypatch.setattr(
        cc.adk,
        "golden_07_open_disputes_for_customer",
        lambda customer_id: {
            "rows": [
                {"dispute_id": "DSP00033", "merchant_name": "Taberna Do Monte", "disputed_amount": "15.11", "regulation_path": "REG_E", "provisional_credit_issued": False},
            ]
        },
    )
    monkeypatch.setattr(
        cc.adk,
        "stage_dispute_filing_tool",
        lambda **kwargs: {"status": "STAGED", "action": "DISPUTE_FILED"},
    )
    res = cc.evaluate_chat_turn("CUST00001", "I lost my wallet in Lisbon, file a dispute and provisional credit")
    assert res["intent"] == "DISPUTES"
    assert "Dispute Specialist Agent (09)" in res["text"]
    assert "Regulation E" in res["text"]
    assert "DSP00033" in res["text"]


def test_why_nothing_working_intent(monkeypatch):
    monkeypatch.setattr(cc.adk, "golden_14_customer_cards_and_wallets", lambda cid: {"rows": []})
    fake_bank = MagicMock()
    fake_bank.retrieve.return_value = []
    monkeypatch.setattr(cc, "shared_memory_bank_client", lambda: fake_bank)

    res = cc.evaluate_chat_turn("CUST00001", "why is nothing working?")
    assert res["intent"] == "HOLISTIC_CAUSAL_SYNTHESIS"
    assert "Suspicious Travel Velocity Alert" in res["text"]
    assert "Apple Pay" in res["text"]
    assert "0513" in res["text"]


def test_customer_chat_api(test_client, monkeypatch):
    monkeypatch.setattr(cc.adk, "customer_display_name", lambda cid: "Priya Vantalis")
    monkeypatch.setattr(cc.adk, "golden_13_customer_accounts_and_balances", lambda cid: {"rows": []})

    resp = test_client.post("/api/customer-chat", json={"customer_id": "CUST00001", "message": "hi"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["intent"] == "GREETING"
    assert "Priya Vantalis" in data["text"]


def test_customer_chat_stream_api(test_client, monkeypatch):
    monkeypatch.setattr(cc.adk, "customer_display_name", lambda cid: "Priya Vantalis")
    monkeypatch.setattr(cc.adk, "golden_13_customer_accounts_and_balances", lambda cid: {"rows": []})

    resp = test_client.get("/api/customer-chat/stream?customer_id=CUST00001&prompt=hi")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["Content-Type"]
    content = resp.get_data(as_text=True)
    assert "data: " in content
    assert '"type": "thought"' in content
    assert '"type": "chunk"' in content
    assert '"type": "complete"' in content


def test_pleasantry_intent(monkeypatch):
    monkeypatch.setattr(cc.adk, "customer_display_name", lambda cid: "Priya Vantalis")
    res = cc.evaluate_chat_turn("CUST00001", "how are you?")
    assert res["intent"] == "CONVERSATIONAL"
    assert "doing well" in res["text"]
    assert "Priya Vantalis" in res["text"]


def test_out_of_scope_guardrail():
    res = cc.evaluate_chat_turn("CUST00001", "what is the weather like in New York?")
    assert res["intent"] == "OUT_OF_SCOPE"
    assert "dedicated JPMC Consumer Banking Assistant" in res["text"]
    assert "weather" in res["text"]


def test_card_unlock_tool_call(monkeypatch):
    monkeypatch.setattr(
        cc.adk,
        "golden_14_customer_cards_and_wallets",
        lambda cid: {
            "rows": [
                {"card_id": "CARD00002", "last_four": "0513", "status": "FROZEN", "product_name": "Sapphire"}
            ]
        },
    )
    monkeypatch.setattr(
        cc.adk,
        "execute_one_click_card_unlock",
        lambda **kwargs: {
            "action_board_item": "CARD_UNLOCKED (AUDIT_STAGED)",
            "audit_session": {"value": "projects/123/sessions/456"},
        },
    )
    res = cc.evaluate_chat_turn("CUST00001", "unlock card ending 0513")
    assert res["intent"] == "CARD_UNLOCK"
    assert "0513" in res["text"]
    assert "Action Staged" in res["text"]
    assert "citations" in res
    assert "execute_one_click_card_unlock" in res["citations"]["tool_calls"]


def test_card_freeze_tool_call(monkeypatch):
    monkeypatch.setattr(
        cc.adk,
        "golden_14_customer_cards_and_wallets",
        lambda cid: {
            "rows": [
                {"card_id": "CARD00005", "last_four": "2758", "status": "ACTIVE", "product_name": "Virtual Card"}
            ]
        },
    )
    monkeypatch.setattr(
        cc.adk,
        "execute_fraud_card_control_or_escalate",
        lambda **kwargs: {"status": "SUCCESS", "action": "FREEZE_CARD"},
    )
    res = cc.evaluate_chat_turn("CUST00001", "lock card ending 2758")
    assert res["intent"] == "CARD_FREEZE"
    assert "2758" in res["text"]
    assert "FREEZE_CARD" in res["text"]
    assert "citations" in res
    assert "execute_fraud_card_control_or_escalate" in res["citations"]["tool_calls"]


def test_disputes_api(test_client, monkeypatch):
    monkeypatch.setattr(
        cc.adk,
        "golden_07_open_disputes_for_customer",
        lambda cid: {
            "rows": [
                {"dispute_id": "DSP00033", "merchant_name": "Tejo Taxis", "disputed_amount": "26.30", "regulation_path": "REG_E", "status": "PROVISIONAL_CREDIT_ISSUED"}
            ]
        },
    )
    resp = test_client.get("/api/disputes?customer_id=CUST00001")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "SUCCESS"
    assert data["active_count"] == 1
    assert data["active_disputes"][0]["dispute_id"] == "DSP00033"
