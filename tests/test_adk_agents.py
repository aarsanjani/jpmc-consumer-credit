import pytest
from backend.adk_agents import list_all_agents, CHANNEL_SESSION_TOKENS

def test_list_all_agents_count_and_names():
    """Verify list_all_agents returns exactly the 8 specified agents."""
    agents = list_all_agents()
    assert len(agents) == 8
    
    expected_names = {
        "fraud_monitoring_agent",
        "telephony_ivr_agent",
        "mobile_app_agent",
        "web_portal_agent",
        "branch_support_agent",
        "claim_veracity_validator_agent",
        "memory_bank_audit_agent",
        "consumer_credit_synthesizer_agent"
    }
    actual_names = {agent.name for agent in agents}
    assert actual_names == expected_names

def test_channel_session_tokens_coverage():
    """Verify CHANNEL_SESSION_TOKENS catches all the seeded spelling aliases."""
    all_tokens = set()
    for tokens in CHANNEL_SESSION_TOKENS.values():
        all_tokens.update(tokens)
        
    expected_aliases = {
        "IVR",
        "MOBILE_APP",
        "WEB_CHAT",
        "FRAUD_OPS_OUTBOUND",
        "BRANCH"
    }
    for alias in expected_aliases:
        assert alias in all_tokens
