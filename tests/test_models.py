import pytest
import backend.models as models

def test_models_exist():
    """Ensure the genuine enums remain."""
    assert hasattr(models, "BankChannel")
    assert hasattr(models, "RegulationPath")
    assert hasattr(models, "ClaimStatus")
    assert hasattr(models, "GovernanceAction")

def test_deleted_models_are_gone():
    """Ensure that the pre-rework models that defaulted everything are gone and cannot silently return."""
    for dead_name in [
        "PolicyRule", 
        "EntityNode", 
        "SynthesisResult", 
        "VeracityStatus", 
        "AdminCardReviewCase",
        "CustomerContext",
        "RelevantAvenueCheck"
    ]:
        assert not hasattr(models, dead_name), f"{dead_name} must remain deleted."
