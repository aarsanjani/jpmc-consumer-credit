from backend.provenance import (
    measured, unmeasured, derived, reported, identity, Origin, derive, NOT_RECORDED
)

def test_unmeasured_renders_as_nr():
    """An unmeasured value must render as 'n/r', never as a plausible default."""
    val = unmeasured("timeout on connection")
    assert val.render() == NOT_RECORDED
    assert str(val) == NOT_RECORDED
    assert val.is_real is False

def test_derive_propagates_unmeasured():
    """Derive must poison the result if any input is unmeasured, avoiding silent default maths."""
    a = measured(10)
    b = unmeasured("database down")
    
    # 10 + unknown should be unknown, not 10 or error.
    result = derive(lambda x, y: x + y, a, b)
    
    assert result.is_real is False
    assert result.origin == Origin.UNMEASURED
    assert result.render() == NOT_RECORDED
    assert "database down" in result.reason

def test_derive_computes_real_values():
    """Derive successfully computes a value when all inputs are real."""
    a = measured(10)
    b = reported(5)
    
    result = derive(lambda x, y: x + y, a, b, unit="ms")
    
    assert result.is_real is True
    assert result.origin == Origin.DERIVED
    assert result.render() == 15
    assert result.unit == "ms"

def test_identity_is_not_real_but_renders_value():
    """Identity represents a static identifier, not a real measurement, but renders its value."""
    val = identity("CUST0001")
    assert val.is_real is False
    assert val.origin == Origin.STATIC
    assert val.value == "CUST0001"
