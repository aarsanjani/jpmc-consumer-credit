import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from google.cloud import bigquery

import backend.adk_agents as adk
from backend.knowledge_catalog import Decision
from backend.models import AgentId
from backend.provenance import ProvenanceError

CUSTOMER_QUERIES = ["13", "14", "15", "16", "17", "18", "19"]
DEMO_NOON = datetime(2031, 3, 15, 12, tzinfo=timezone.utc)


@dataclass(frozen=True)
class RecordedQuery:
    sql: str
    types: dict[str, str]
    values: dict[str, Any]


class AllowAllCatalog:
    """Stands in for CatalogGovernor: every table and column is allowed and no sibling is discovered."""

    client = SimpleNamespace(search_entries=lambda query: [])

    def check_table(self, table: str) -> Decision:
        return Decision(allowed=True, action="ALLOW", subject=table)

    def check_column(self, table: str, column: str) -> Decision:
        return Decision(allowed=True, action="ALLOW", subject=f"{table}.{column}")


@pytest.fixture
def bigquery_queries(monkeypatch) -> list[RecordedQuery]:
    queries: list[RecordedQuery] = []

    class RecordingClient:
        def __init__(self, project: str) -> None:
            self.project = project

        def query(self, sql: str, job_config: bigquery.QueryJobConfig) -> SimpleNamespace:
            params = job_config.query_parameters
            queries.append(RecordedQuery(
                sql=sql,
                types={p.name: getattr(p, "type_", "ARRAY") for p in params},
                values={p.name: getattr(p, "value", None) for p in params},
            ))
            return SimpleNamespace(result=lambda: [], job_id="fake-job", total_bytes_billed=0)

    monkeypatch.setattr(bigquery, "Client", RecordingClient)
    monkeypatch.setattr(adk, "GLOBAL_CATALOG_GOVERNOR", AllowAllCatalog())
    # A fresh cache, so this fake's empty discovery results never leak into other tests.
    monkeypatch.setattr(adk, "_SIBLING_TABLE_CACHE", {})
    monkeypatch.setenv("DEMO_AS_OF_DATE", "2031-03-15")
    return queries


def sql_body(query_id: str) -> str:
    """The golden SQL without its comments, so the header's examples are not mistaken for code."""
    return re.sub(r"--[^\n]*", "", adk.load_golden_sql_text(query_id))


def tables_read(query_id: str) -> set[str]:
    return set(re.findall(r"`[\w-]+\.\w+\.(\w+)`", sql_body(query_id)))


def parameters_used(query_id: str) -> set[str]:
    return set(re.findall(r"@(\w+)", sql_body(query_id)))


@pytest.mark.parametrize("column", ["pan_full", "pan_masked", "ssn_last4"])
@pytest.mark.parametrize("query_id", CUSTOMER_QUERIES)
def test_customer_query_sql_never_mentions_a_restricted_column(query_id, column):
    assert column not in adk.load_golden_sql_text(query_id).lower()


@pytest.mark.parametrize("query_id", CUSTOMER_QUERIES)
def test_contract_governs_every_table_its_sql_reads(query_id):
    assert tables_read(query_id) <= set(adk.GOLDEN_QUERY_CONTRACTS[query_id].governed_tables)


@pytest.mark.parametrize("query_id", CUSTOMER_QUERIES)
def test_contract_declares_exactly_the_parameters_its_sql_uses(query_id):
    assert parameters_used(query_id) == set(adk.GOLDEN_QUERY_CONTRACTS[query_id].required_parameters)


@pytest.mark.parametrize(("query_id", "agents"), [
    ("13", {AgentId.CONSUMER_CREDIT_SYNTHESIZER}),
    ("14", {AgentId.MOBILE_APP, AgentId.CONSUMER_CREDIT_SYNTHESIZER}),
    ("15", {AgentId.CONSUMER_CREDIT_SYNTHESIZER}),
    ("16", {AgentId.CONSUMER_CREDIT_SYNTHESIZER}),
    ("17", {AgentId.WEB_PORTAL, AgentId.CONSUMER_CREDIT_SYNTHESIZER, AgentId.DISPUTE_SPECIALIST}),
    ("18", {AgentId.CONSUMER_CREDIT_SYNTHESIZER}),
    ("19", {AgentId.WEB_PORTAL, AgentId.CONSUMER_CREDIT_SYNTHESIZER, AgentId.DISPUTE_SPECIALIST}),
])
def test_contract_authorizes_only_the_agreed_agents(query_id, agents):
    assert set(adk.GOLDEN_QUERY_CONTRACTS[query_id].authorized_agents) == agents


@pytest.mark.parametrize("query_id", CUSTOMER_QUERIES)
def test_unlisted_agent_is_refused_before_bigquery_is_called(bigquery_queries, query_id):
    payload = adk.execute_golden_query_contract(query_id, AgentId.FRAUD_MONITORING, params={})

    assert payload["allowed"] is False
    assert bigquery_queries == []


@pytest.mark.parametrize(("query_id", "args", "types"), [
    ("13", ["CUST00009"], {"customer_id": "STRING"}),
    ("14", ["CUST00009"], {"customer_id": "STRING"}),
    ("15", ["CUST00009"],
     {"customer_id": "STRING", "start_date": "DATE", "end_date": "DATE", "top_merchant_limit": "INT64"}),
    ("16", ["CUST00009", "ACCT000009"],
     {"customer_id": "STRING", "account_id": "STRING", "period_start": "DATE", "period_end": "DATE",
      "row_limit": "INT64"}),
    ("17", ["CUST00009"], {"customer_id": "STRING"}),
    ("18", ["CUST00009"], {"customer_id": "STRING", "as_of_date": "DATE"}),
    ("19", ["CUST00009"], {"customer_id": "STRING"}),
])
def test_wrapper_runs_its_contract_sql_with_typed_parameters(bigquery_queries, query_id, args, types):
    wrapper = getattr(adk, adk.GOLDEN_QUERY_CONTRACTS[query_id].tool_name)

    payload = wrapper(*args)

    assert payload["ad_hoc_sql_used"] is False
    assert bigquery_queries[0].sql == adk.load_golden_sql_text(query_id)
    assert bigquery_queries[0].types == types


@pytest.mark.parametrize(("call", "param", "expected"), [
    pytest.param(lambda: adk.golden_15_spend_summary("CUST00009"), "start_date", date(2031, 3, 9), id="15-start"),
    pytest.param(lambda: adk.golden_15_spend_summary("CUST00009"), "end_date", date(2031, 3, 15), id="15-end"),
    pytest.param(lambda: adk.golden_16_statement_transactions("CUST00009", "ACCT000009"),
                 "period_start", date(2031, 2, 14), id="16-start"),
    pytest.param(lambda: adk.golden_16_statement_transactions("CUST00009", "ACCT000009"),
                 "period_end", date(2031, 3, 15), id="16-end"),
    pytest.param(lambda: adk.golden_18_customer_contact_profile("CUST00009"),
                 "as_of_date", date(2031, 3, 15), id="18"),
    pytest.param(lambda: adk.golden_02_recent_authorizations_for_card("CARD00009"), "as_of_ts", DEMO_NOON, id="02"),
    pytest.param(lambda: adk.golden_03_suspicious_activity_on_card_window("CARD00009"),
                 "window_start_ts", datetime(2031, 3, 1, tzinfo=timezone.utc), id="03-start"),
    pytest.param(lambda: adk.golden_03_suspicious_activity_on_card_window("CARD00009"),
                 "window_end_ts", DEMO_NOON, id="03-end"),
    pytest.param(lambda: adk.golden_04_geo_impossible_travel("CARD00009"), "as_of_ts", DEMO_NOON, id="04"),
    pytest.param(lambda: adk.golden_07_open_disputes_for_customer("CUST00009"),
                 "as_of_date", date(2031, 3, 15), id="07"),
    pytest.param(lambda: adk.golden_09_provisional_credit_due(), "as_of_date", date(2031, 3, 15), id="09"),
    pytest.param(lambda: adk.golden_10_wallet_tokens_active_on_card("CARD00009"), "as_of_ts", DEMO_NOON, id="10"),
    pytest.param(lambda: adk.golden_11_dispute_event_timeline("DSP00099"),
                 "as_of_date", date(2031, 3, 15), id="11"),
])
def test_wrapper_measures_dates_from_the_demo_clock(bigquery_queries, call, param, expected):
    call()

    assert bigquery_queries[0].values[param] == expected


@pytest.mark.parametrize(("dates", "window"), [
    ({"start_date": "2031-01-01", "end_date": "2031-01-31"}, (date(2031, 1, 1), date(2031, 1, 31))),
    ({"end_date": "2031-01-31"}, (date(2031, 1, 25), date(2031, 1, 31))),
    ({"start_date": "2031-03-10"}, (date(2031, 3, 10), date(2031, 3, 15))),
    ({"start_date": "2031-03-15", "end_date": "2031-03-15"}, (date(2031, 3, 15), date(2031, 3, 15))),
], ids=["both", "end-only", "start-only", "single-day"])
def test_spend_summary_binds_the_window_it_was_asked_for(bigquery_queries, dates, window):
    adk.golden_15_spend_summary("CUST00009", **dates)

    assert (bigquery_queries[0].values["start_date"], bigquery_queries[0].values["end_date"]) == window


def test_statement_binds_the_period_it_was_asked_for(bigquery_queries):
    adk.golden_16_statement_transactions("CUST00009", "ACCT000009", period_start="2031-02-01", period_end="2031-02-28")

    assert (bigquery_queries[0].values["period_start"], bigquery_queries[0].values["period_end"]) == (
        date(2031, 2, 1), date(2031, 2, 28))


@pytest.mark.parametrize("call", [
    pytest.param(lambda: adk.golden_15_spend_summary("CUST00009", start_date="last week"), id="15-words"),
    pytest.param(lambda: adk.golden_15_spend_summary("CUST00009", end_date="15/03/2031"), id="15-day-first"),
    pytest.param(lambda: adk.golden_15_spend_summary("CUST00009", start_date="2031-03-20", end_date="2031-03-01"),
                 id="15-backwards"),
    pytest.param(lambda: adk.golden_16_statement_transactions("CUST00009", "ACCT000009", period_end="2031-13-01"),
                 id="16-no-such-month"),
])
def test_bad_dates_go_back_to_the_caller_without_a_query(bigquery_queries, call):
    payload = call()

    assert payload["allowed"] is False
    assert "YYYY-MM-DD" in payload["error"]
    assert bigquery_queries == []


def test_missing_demo_clock_raises_instead_of_reaching_the_caller_as_a_bad_date(bigquery_queries, monkeypatch):
    monkeypatch.delenv("DEMO_AS_OF_DATE")

    with pytest.raises(ProvenanceError, match="DEMO_AS_OF_DATE"):
        adk.golden_15_spend_summary("CUST00009")


def test_malformed_demo_clock_raises(bigquery_queries, monkeypatch):
    monkeypatch.setenv("DEMO_AS_OF_DATE", "15/03/2031")

    with pytest.raises(ValueError, match="DEMO_AS_OF_DATE must be a YYYY-MM-DD date"):
        adk.golden_18_customer_contact_profile("CUST00009")
