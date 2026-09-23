"""
Provenance: a value must be able to say where it came from.

The failure this module exists to prevent is specific and it was endemic in this
repository before the rewrite. Code would try to measure something, fail, and
substitute a plausible constant:

    except Exception:
        p_tok = 195          # backend/custom_agent_comparison.py:170, on `main`
        c_tok = 240

The number then travelled all the way to the UI, where it was indistinguishable
from a measurement. A broken run scored the same as a working one, and the
headline verdict -- "ADK reduces integration boilerplate by ~73%" -- was arithmetic
over two such constants.

The fix is to make "not measured" a *representable value* rather than something a
programmer has to remember to handle. `unmeasured()` carries a reason, renders as
``n/r``, and refuses to be silently coerced into a number.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class Origin(str, Enum):
    """How a value came to exist."""

    MEASURED = "MEASURED"
    """Observed this run: a timer, a response field, a row count."""

    REPORTED = "REPORTED"
    """Stated by the remote service in its response (e.g. token usage)."""

    DERIVED = "DERIVED"
    """Computed from other Provenanced values, all of which were themselves real."""

    UNMEASURED = "UNMEASURED"
    """We tried and could not, or never tried. Renders as `n/r`."""

    STATIC = "STATIC"
    """A constant baked into the source. Legitimate for identity and labels;
    an offence for anything a reader would take as a result."""


NOT_RECORDED = "n/r"


@dataclass(frozen=True)
class Provenanced:
    """A value together with the story of how it was obtained."""

    value: Any
    origin: Origin
    reason: Optional[str] = None
    unit: Optional[str] = None
    source: Optional[str] = None

    @property
    def is_real(self) -> bool:
        """True if this value reflects something that actually happened."""
        return self.origin in (Origin.MEASURED, Origin.REPORTED, Origin.DERIVED)

    def render(self) -> Any:
        """The value as it should appear to a human.

        Unmeasured values render as ``n/r`` -- never as ``0``, never as a
        plausible-looking default. A zero is a claim; ``n/r`` is an admission.
        """
        if not self.is_real:
            return NOT_RECORDED
        return self.value

    def to_dict(self) -> dict:
        out = {
            "value": self.render(),
            "origin": self.origin.value,
            "measured": self.is_real,
        }
        if self.unit:
            out["unit"] = self.unit
        if self.reason:
            out["reason"] = self.reason
        if self.source:
            out["source"] = self.source
        return out

    def __str__(self) -> str:
        rendered = self.render()
        if rendered == NOT_RECORDED:
            return NOT_RECORDED
        return f"{rendered}{self.unit or ''}"


def measured(value: Any, unit: Optional[str] = None, source: Optional[str] = None) -> Provenanced:
    """A value observed during this run."""
    return Provenanced(value=value, origin=Origin.MEASURED, unit=unit, source=source)


def reported(value: Any, unit: Optional[str] = None, source: Optional[str] = None) -> Provenanced:
    """A value the remote service told us."""
    return Provenanced(value=value, origin=Origin.REPORTED, unit=unit, source=source)


def derived(value: Any, unit: Optional[str] = None, source: Optional[str] = None) -> Provenanced:
    """A value computed from other real values."""
    return Provenanced(value=value, origin=Origin.DERIVED, unit=unit, source=source)


def unmeasured(reason: str, unit: Optional[str] = None) -> Provenanced:
    """A value we do not have.

    The reason is mandatory. "We could not measure this" is only useful to a
    reader if it is followed by "because".
    """
    return Provenanced(value=None, origin=Origin.UNMEASURED, reason=reason, unit=unit)


def identity(value: Any, note: str = "identifier, not a result") -> Provenanced:
    """A constant that is legitimately constant -- a name, a label, an id.

    Marked ``STATIC`` but waived: the audit in `summarize` separates these from
    static values that a reader would mistake for a measurement.
    """
    return Provenanced(value=value, origin=Origin.STATIC, reason=note)


def derive(
    fn: Callable[..., Any],
    *inputs: Provenanced,
    unit: Optional[str] = None,
) -> Provenanced:
    """Compute a value from other Provenanced values, propagating honesty.

    If any input is unmeasured the result is unmeasured, and the reason names
    which input was missing. This is the property that stops a single silent
    fallback from laundering itself into a confident derived statistic -- which
    is exactly how ``~73%`` happened.
    """
    missing = [i for i in inputs if not i.is_real]
    if missing:
        reasons = "; ".join(m.reason or "unmeasured input" for m in missing)
        return unmeasured(f"derived value unavailable: {reasons}", unit=unit)
    return derived(fn(*[i.value for i in inputs]), unit=unit)


def count_source_lines(obj: Any) -> Provenanced:
    """Count the source lines of a function or class, at runtime.

    Used for the "how much integration code does this framework need" comparison.
    Counting at runtime rather than writing a number in a dict means editing the
    code changes the figure, and the figure cannot drift into marketing.
    """
    try:
        lines, _ = inspect.getsourcelines(obj)
        body = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        return measured(len(body), unit=" LOC", source=f"inspect.getsourcelines({getattr(obj, '__name__', obj)})")
    except (OSError, TypeError) as exc:
        return unmeasured(f"source not available for {obj!r}: {exc}", unit=" LOC")


@dataclass
class ProvenanceLedger:
    """Collects Provenanced values over a run so a report can be audited.

    The point is not the values, it is the *summary*: how many of the numbers on
    this page were actually measured.
    """

    entries: dict = field(default_factory=dict)

    def record(self, key: str, value: Provenanced) -> Provenanced:
        self.entries[key] = value
        return value

    def summarize(self) -> dict:
        real = [k for k, v in self.entries.items() if v.is_real]
        unmeasured_keys = [
            k for k, v in self.entries.items() if v.origin is Origin.UNMEASURED
        ]
        static_unwaived = [
            k
            for k, v in self.entries.items()
            if v.origin is Origin.STATIC and v.reason is None
        ]
        total = len(self.entries) or 1
        return {
            "total_values": len(self.entries),
            "measured": len(real),
            "unmeasured": len(unmeasured_keys),
            "unmeasured_keys": sorted(unmeasured_keys),
            "static_unwaived_keys": sorted(static_unwaived),
            "measured_fraction": round(len(real) / total, 4),
        }

    def render(self) -> dict:
        return {k: v.to_dict() for k, v in self.entries.items()}
