import itertools

import pytest
from flask import Flask

from backend import adk_agents, ui_insights
from backend.knowledge_catalog import Decision
from backend.ui_insights import (
    ChannelAgent,
    ab_for_agent,
    catalog_verdicts,
    channel_agents,
    count_on_topic,
    label_rows,
    query_provenance,
    rows_missing_from,
    run_topic_ab,
)


def _memory(memory_id, fact, *topics):
    return {"memory": {
        "name": "projects/p/locations/l/reasoningEngines/e/memories/%s" % memory_id,
        "fact": fact,
        "topics": [{"customMemoryTopicLabel": t} for t in topics],
    }}


class FakeBank:
    """Returns `unfiltered` without a filter and `filtered` with one, and records calls."""

    def __init__(self, unfiltered, filtered, configured=("CARD_LIFECYCLE", "TRAVEL_STATUS", "DISPUTE_HISTORY")):
        self._unfiltered = unfiltered
        self._filtered = filtered
        self._configured = configured
        self.calls = []

    def memory_topics(self, refresh=False):
        return [{"label": label, "kind": "CUSTOM"} for label in self._configured]

    def retrieve(self, scope, query=None, top_k=5, memory_filter=None):
        self.calls.append({"scope": scope, "query": query, "top_k": top_k, "memory_filter": memory_filter})
        return self._filtered if memory_filter else self._unfiltered


def _ticking_clock(step_s=0.010):
    ticks = itertools.count(step=step_s)
    return lambda: next(ticks)


FRAUD = ChannelAgent("01_fraud_monitoring_agent", "fraud_monitoring_agent",
                     ("CARD_LIFECYCLE", "TRAVEL_STATUS"), "card lock query")

UNFILTERED = [
    _memory("m1", "Card 4417 frozen after theft", "CARD_LIFECYCLE"),
    _memory("m2", "Disputed a Lisbon charge", "DISPUTE_HISTORY"),
    _memory("m3", "Travelling to Lisbon 1-12 Sep", "TRAVEL_STATUS"),
]
FILTERED = [
    _memory("m1", "Card 4417 frozen after theft", "CARD_LIFECYCLE"),
    _memory("m3", "Travelling to Lisbon 1-12 Sep", "TRAVEL_STATUS"),
    _memory("m4", "Replacement card issued", "CARD_LIFECYCLE"),
]


@pytest.mark.parametrize(
    ("topics", "expected"),
    [(["CARD_LIFECYCLE"], 1), (["CARD_LIFECYCLE", "DISPUTE_HISTORY"], 2), (["CONTACT_PREFERENCE"], 0)],
    ids=["one-topic", "two-topics", "no-match"],
)
def test_count_on_topic_counts_memories_with_any_agent_topic(topics, expected):
    labelled = label_rows(UNFILTERED[:2], topics)

    assert count_on_topic(labelled) == expected


def test_label_rows_marks_memory_without_topics_as_off_topic():
    labelled = label_rows([{"memory": {"name": "x/memories/m9", "fact": "no labels"}}], ["CARD_LIFECYCLE"])

    assert labelled == [{"memory_id": "m9", "fact": "no labels", "topics": [], "on_topic": False}]


def test_label_rows_truncates_long_facts():
    labelled = label_rows([_memory("m1", "x" * 500, "CARD_LIFECYCLE")], ["CARD_LIFECYCLE"])

    assert len(labelled[0]["fact"]) == ui_insights.FACT_PREVIEW_CHARS
    assert labelled[0]["fact"].endswith("…")


def test_rows_missing_from_matches_by_memory_id():
    baseline = label_rows(UNFILTERED, FRAUD.topics)
    filtered = label_rows(FILTERED, FRAUD.topics)

    dropped = rows_missing_from(baseline, filtered)

    assert [row["memory_id"] for row in dropped] == ["m2"]
    assert dropped[0]["topics"] == ["DISPUTE_HISTORY"]


def test_ab_for_agent_sends_same_query_and_scope_on_both_sides():
    bank = FakeBank(UNFILTERED, FILTERED)

    ab_for_agent(bank, "CUST00001", FRAUD, top_k=5, clock=_ticking_clock())

    assert [c["memory_filter"] is None for c in bank.calls] == [True, False]
    assert {c["query"] for c in bank.calls} == {"card lock query"}
    assert {tuple(c["scope"].items()) for c in bank.calls} == {(("customer_id", "CUST00001"),)}


def test_ab_for_agent_reports_counts_dropped_and_added():
    bank = FakeBank(UNFILTERED, FILTERED)

    row = ab_for_agent(bank, "CUST00001", FRAUD, clock=_ticking_clock())

    assert row["baseline"]["on_topic"]["value"] == 2
    assert row["baseline"]["returned"]["value"] == 3
    assert row["filtered"]["on_topic"]["value"] == 3
    assert [r["memory_id"] for r in row["dropped"]["value"]] == ["m2"]
    assert [r["memory_id"] for r in row["added"]["value"]] == ["m4"]


def test_ab_for_agent_without_configured_topics_leaves_filtered_side_unmeasured():
    bank = FakeBank(UNFILTERED, FILTERED, configured=("CONTACT_PREFERENCE",))

    row = ab_for_agent(bank, "CUST00001", FRAUD, clock=_ticking_clock())

    assert len(bank.calls) == 1
    assert row["filtered"]["value"] == "n/r"
    assert row["extra_ms"]["value"] == "n/r"


def test_run_topic_ab_totals_only_measured_agents():
    bank = FakeBank(UNFILTERED, FILTERED)
    unknown = ChannelAgent("02_x", "x", ("CONTACT_PREFERENCE",), "q")

    payload = run_topic_ab(bank, "CUST00001", [FRAUD, unknown], clock=_ticking_clock())

    totals = payload["totals"]
    assert totals["measured_agents"]["value"] == 1
    assert (totals["baseline_on_topic"]["value"], totals["baseline_returned"]["value"]) == (2, 3)
    assert totals["baseline_other_topics"]["value"] == 1
    assert totals["filtered_on_topic"]["value"] == 3


def test_channel_agents_reads_each_agents_query_from_the_shared_constant():
    agents = channel_agents()

    assert [a.agent_id[:2] for a in agents] == ["01", "02", "03", "04", "05"]
    assert [a.query for a in agents] == [adk_agents.CHANNEL_RETRIEVAL_QUERIES[a.name] for a in agents]


def test_query_provenance_names_the_constant_entry():
    assert query_provenance("mobile_app_agent") == "adk_agents.CHANNEL_RETRIEVAL_QUERIES[mobile_app_agent]"


class FakeGovernor:
    strict = True

    def check_table(self, table, dataset=None):
        return Decision(allowed=True, action="ALLOW", subject=table, evidence="lifecycle-status.status = ACTIVE")

    def check_column(self, table, column, dataset=None):
        return Decision(allowed=False, action="REFUSE", subject="%s.%s" % (table, column),
                        evidence="sensitivity-class.agent_behavior = REFUSE")


def test_catalog_verdicts_checks_columns_for_dotted_names_and_tables_otherwise():
    verdicts = catalog_verdicts(FakeGovernor(), ["disputes", "cards.pan_full"])

    assert [(v["kind"], v["decision"]["value"]["action"]) for v in verdicts] == [
        ("table", "ALLOW"), ("column", "REFUSE")]


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(ui_insights.insights)
    return app.test_client()


@pytest.mark.parametrize(
    "url",
    ["/api/memory/topic-ab?customer_id=bad%20id", "/api/catalog/verdicts", "/api/catalog/verdicts?names=a;drop"],
    ids=["bad-customer", "no-names", "bad-name"],
)
def test_routes_reject_bad_input_before_calling_services(client, url):
    response = client.get(url)

    assert response.status_code == 400


def test_golden_contracts_route_lists_tables_per_query(client):
    response = client.get("/api/golden/contracts")

    contracts = {c["id"]: c for c in response.get_json()["contracts"]}
    assert "authorizations" in contracts["03_suspicious_activity_on_card_window"]["tables"]
