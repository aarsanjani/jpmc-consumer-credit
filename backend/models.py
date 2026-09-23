"""
Shared data models.

Trimmed hard. Most of what was here described the fabricated demo -- a
`CustomerContext` whose every field defaulted to `"Alex Morgan"` and card
`"4821"`, a `SynthesisResult` with `confidence_score: float = 0.99` as a
*schema default*, and `RelevantAvenueCheck` whose field descriptions carried
prompt instructions like "never include unrelated transactions from other
cities".

Those defaults were the problem. A schema default of 0.99 means every object
claims high confidence unless something remembers to overwrite it, and nothing
did. A confidence that is never computed should not be representable.

What remains are the shapes that outlive the demo: the service-facing records
(in `memory_bank.py`, `knowledge_catalog.py`, `ground_truth.py` as dataclasses
next to the code that builds them) and the small enums below, which are genuine
closed vocabularies rather than free strings with a comment.
"""

from __future__ import annotations

from enum import Enum


class BankChannel(str, Enum):
    """Contact channels, matching the values the warehouse actually stores.

    Checked against `SELECT DISTINCT channel FROM contact_events` rather than
    invented. A channel constant that does not match the data makes a producer
    return an empty result *silently*, which reads as "no activity on this
    channel" rather than "wrong constant" -- the most expensive kind of wrong.
    """

    IVR = "IVR"
    MOBILE_APP = "MOBILE_APP"
    WEB_CHAT = "WEB_CHAT"
    BRANCH = "BRANCH"
    FRAUD_OPS_OUTBOUND = "FRAUD_OPS_OUTBOUND"


class RegulationPath(str, Enum):
    """The regulation governing a dispute.

    Two instruments on one account can fall under different regulations with
    different obligations, so this is read per dispute from
    `disputes.regulation_path` and never assumed from the account.
    """

    REG_E = "REG_E"
    REG_Z = "REG_Z"


class ClaimStatus(str, Enum):
    """The outcome of checking a stated claim against the system of record."""

    CORROBORATED = "CORROBORATED"
    """The warehouse agrees."""

    CONTRADICTED = "CONTRADICTED"
    """The warehouse disagrees. The warehouse is correct; the claim is still a
    faithful record of what was said."""

    UNVERIFIABLE = "UNVERIFIABLE"
    """No authoritative source bears on this claim. Distinct from corroborated,
    and collapsing the two is how an unchecked claim becomes a verified one."""


class GovernanceAction(str, Enum):
    """What the catalog instructs an agent to do.

    Mirrors the `agent_behavior` field of the `sensitivity-class` aspect. The
    human-readable `level` (CONFIDENTIAL, RESTRICTED) is deliberately *not*
    modelled here: a label is for a person to read, and acting on it rather than
    on the explicit instruction is how a catalog fills up with severe-sounding
    words that no system honours.
    """

    ALLOW = "ALLOW"
    ALLOW_TO_OWNER_ONLY = "ALLOW_TO_OWNER_ONLY"
    REFUSE = "REFUSE"
    UNKNOWN = "UNKNOWN"
    """The catalog could not be reached. Not the same as ALLOW, and must never
    be treated as such."""
