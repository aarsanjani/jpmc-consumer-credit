import pytest
from backend.provenance import (
    tag,
    live,
    seeded,
    computed,
    static,
    identity,
    unmeasured,
    value_of,
    is_tagged,
    walk,
    summarize,
    require_env,
    ProvenanceError,
    STATIC,
    LIVE_API,
    SEEDED,
    COMPUTED,
    NOT_RECORDED
)

def test_tags_wrap_value():
    """Verify that provenance tags correctly wrap a value and carry their tag name."""
    assert live(42, "api")["_provenance"]["tag"] == LIVE_API
    assert seeded("data", "bq")["_provenance"]["tag"] == SEEDED
    assert computed(100.0, "calc")["_provenance"]["tag"] == COMPUTED
    assert static("hello", "source")["_provenance"]["tag"] == STATIC

def test_identity_is_waived_static():
    """Verify that identity() is considered STATIC but is marked as waived."""
    tagged = identity("agent1", "source", "im_an_agent")
    assert tagged["value"] == "agent1"
    prov = tagged["_provenance"]
    assert prov["tag"] == STATIC
    assert prov["waived"] is True
    assert prov["reason"] == "im_an_agent"

def test_unmeasured_renders_not_recorded():
    """Verify that unmeasured() explicitly returns 'n/r' and not zero or None."""
    tagged = unmeasured("source")
    assert tagged["value"] == NOT_RECORDED
    assert tagged["_provenance"]["tag"] == COMPUTED

def test_value_unwrapping():
    """Verify value_of and is_tagged safely unwrap explicitly tagged values."""
    tagged = computed(99, "calc")
    assert is_tagged(tagged) is True
    assert value_of(tagged) == 99

    # Untagged values pass through
    assert is_tagged(99) is False
    assert value_of(99) == 99
    assert is_tagged({"foo": "bar"}) is False
    assert value_of({"foo": "bar"}) == {"foo": "bar"}

def test_walk_discovers_nested_provenance():
    """Verify walk() finds all provenance structures across dicts and lists."""
    payload = {
        "a": live(1, "source_a"),
        "b": [
            seeded(2, "source_b"),
            {"c": static(3, "source_c")}
        ]
    }
    paths_found = {record["path"]: record["tag"] for record in walk(payload)}
    assert paths_found["$.a"] == LIVE_API
    assert paths_found["$.b[0]"] == SEEDED
    assert paths_found["$.b[1].c"] == STATIC

def test_summarize_flags_offenders():
    """Verify summarize() counts waivers correctly and flags non-waived STATIC values."""
    payload = {
        "good": live(1, "s"),
        "bad1": static(2, "s"),
        "bad2": static(3, "s"),
        "waived1": identity(4, "s", "r"),
    }
    summary = summarize(payload)
    
    assert summary["clean"] is False
    assert summary["counts"][STATIC] == 3
    assert summary["counts"]["STATIC_WAIVED"] == 1
    assert summary["counts"]["STATIC_UNWAIVED"] == 2
    assert len(summary["static_offenders"]) == 2
    assert len(summary["static_waivers"]) == 1

def test_summarize_clean():
    """Verify summarize() returns clean=True when no unwaived STATIC values exist."""
    payload = {
        "good1": live(1, "s"),
        "good2": computed(1, "s"),
        "waived_only": identity(1, "s", "r")
    }
    summary = summarize(payload)
    assert summary["clean"] is True
    assert summary["counts"]["STATIC_UNWAIVED"] == 0

def test_require_env_raises_if_unset(monkeypatch):
    """Verify require_env fails loudly when a critical param is missing."""
    monkeypatch.delenv("missing_var", raising=False)
    with pytest.raises(ProvenanceError) as excinfo:
        require_env("missing_var")
    assert "missing_var is not set" in str(excinfo.value)

def test_require_env_returns_if_set(monkeypatch):
    """Verify require_env returns the environment value when it exists."""
    monkeypatch.setenv("found_var", "foo")
    assert require_env("found_var") == "foo"
