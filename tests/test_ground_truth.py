import pytest
from backend.ground_truth import GroundTruthReader, GroundTruthError, _as_param
from backend.knowledge_catalog import GovernanceDecision
from backend.provenance import Origin

def test_as_param_preserves_bool():
    """_as_param must bind a Python bool as BOOL not INT64 to preventing BigQuery rejection. bool is a subclass of int."""
    param_true = _as_param("is_active", True)
    param_false = _as_param("is_closed", False)
    
    assert param_true.type_ == "BOOL"
    assert param_false.type_ == "BOOL"
    
    param_int = _as_param("count", 42)
    assert param_int.type_ == "INT64"

def test_as_param_datetime_vs_date():
    """_as_param must resolve datetime as DATETIME and date as DATE, respecting subclass hierarchy."""
    import datetime
    dt = datetime.datetime.now()
    d = datetime.date.today()
    param_dt = _as_param("dt", dt)
    param_d = _as_param("d", d)
    assert param_dt.type_ == "DATETIME"
    assert param_d.type_ == "DATE"

def test_unknown_query_raises(mocker):
    """GroundTruthReader must reject unknown query ids to prevent SQL injection by ad-hoc queries."""
    reader = GroundTruthReader()
    with pytest.raises(GroundTruthError, match="unknown golden query"):
        reader.run("SELECT * FROM passwords")

def test_refuse_blocks_read_when_enforced(mocker):
    """A REFUSE verdict from the catalog blocks the BigQuery read and returns an unmeasured read when enforced."""
    reader = GroundTruthReader()
    
    decision = GovernanceDecision(
        resource="users",
        kind="TABLE",
        allowed=False,
        action="REFUSE",
        basis="Because I said so"
    )
    mocker.patch.object(reader.governor, "check_table", return_value=decision)
    
    result = reader.run("customer_360", {"customer_id": "CUST00001"}, enforce_refusals=True)
    
    assert len(result.rows) == 0
    assert result.latency_ms.origin == Origin.UNMEASURED
    assert result.bytes_processed.origin == Origin.UNMEASURED
    assert len(result.refusals) > 0
    assert result.refusals[0].action == "REFUSE"
