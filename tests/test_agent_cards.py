import pytest

from backend.adk_agents import GOLDEN_QUERY_CONTRACTS, list_all_agents

AGENTS = {d.name: d for d in list_all_agents()}


def test_web_portal_card_separates_authorized_from_bound():
    card = AGENTS["web_portal_agent"]

    assert card.authorized_golden_queries == [
        "07_open_disputes_for_customer", "08_dispute_filing_window_check", "09_provisional_credit_due",
        "17_card_replacement_tracking", "19_travel_notices_for_customer"]
    assert card.bound_golden_queries == ["07_open_disputes_for_customer"]


@pytest.mark.parametrize("name", sorted(AGENTS))
def test_card_fields_come_from_contracts(name):
    card = AGENTS[name]
    authorized = {c.sql_filename.removesuffix(".sql") for c in GOLDEN_QUERY_CONTRACTS.values()
                  if card.agent_id in {a.value for a in c.authorized_agents}}

    assert set(card.authorized_golden_queries) == authorized
    assert set(card.bound_golden_queries) <= set(card.authorized_golden_queries)
    assert not any("(" in entry for entry in card.catalog_governance)
