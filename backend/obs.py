"""
obs.py - one place to configure logging for the fraud desk, and one way to trace a turn.

Why this exists
---------------
The desk makes a lot of small remote calls -- Memory Bank retrieves, Dataplex
lookups, BigQuery golden queries -- and when a run looks wrong the question is
always "which call produced this, and what did it actually return". `print`
cannot answer that: it has no level, no timestamp, no correlation, and it cannot
be turned off in production. So this module gives the package:

  * a single logger tree rooted at `ccb`, so one level setting controls everything;
  * a `turn()` context manager that logs entry, exit, elapsed time and outcome,
    and nests, so a run reads as a tree in the terminal;
  * a run id carried in a `contextvars.ContextVar`, so concurrent requests stay
    separable in one log stream.

Level contract
--------------
The levels are chosen so that each one answers a different question. Pick the
one that matches what you are asking, rather than reading DEBUG for everything.

  ERROR    something failed and the caller is getting an exception
  WARNING  something degraded but the run continues -- a catalog refusal, a
           fallback, an empty channel that was expected to have data
  INFO     the flow: one line per agent turn and per tool call, with outcome
           and elapsed time. This is the level you watch a demo at.
  DEBUG    the payloads: bound query parameters, aspect evidence, the memory
           facts that came back, row samples. This is the level you debug at.
  TRACE    (custom, 5) the wire: full request bodies and responses. Very loud.

Usage
-----
    from backend import obs

    obs.configure()                      # honours CCB_LOG_LEVEL, default INFO
    LOG = obs.get_logger(__name__)

    with obs.turn("golden_query", query_id="02") as t:
        rows = run()
        t.detail(row_count=len(rows))

Nothing here configures logging at import. Libraries that configure logging on
import steal the root handler from whoever imported them; `configure()` is an
explicit call that `app.py` and the trace script make.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sys
import time
import threading
import uuid
from typing import Any, Dict, Iterator, Optional

# ---------------------------------------------------------------------------
# Level registry
# ---------------------------------------------------------------------------

TRACE = 5
logging.addLevelName(TRACE, "TRACE")

ROOT_LOGGER_NAME = "ccb"

#: Set once by `configure()`. Read by `is_configured()` so a second call from a
#: different entry point does not stack a second handler onto the same logger.
_CONFIGURED = False

#: The run this log line belongs to. A default is supplied so a log call outside
#: any `turn()` still renders, rather than raising LookupError.
_RUN_ID: contextvars.ContextVar[str] = contextvars.ContextVar("ccb_run_id", default="-")

#: How deeply `turn()` is nested right now, for the tree indent.
_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("ccb_turn_depth", default=0)
# What kind of work a run represents. Background probes and operator turns both
# produce spans; without this the UI cannot tell which run to show as "the turn".
_RUN_KIND: contextvars.ContextVar[str] = contextvars.ContextVar("ccb_run_kind", default="")


def trace(self: logging.Logger, message: str, *args: Any, **kwargs: Any) -> None:
    """`logger.trace(...)`, so TRACE is reachable the same way the others are."""
    if self.isEnabledFor(TRACE):
        self._log(TRACE, message, args, **kwargs)


logging.Logger.trace = trace  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

class _ContextFilter(logging.Filter):
    """Attaches the run id and nesting depth to every record.

    A Filter rather than a LoggerAdapter because the context has to reach
    records emitted by code that never saw an adapter -- a library logger under
    `ccb`, for example.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _RUN_ID.get()
        record.depth = _DEPTH.get()
        return True


class HumanFormatter(logging.Formatter):
    """Indented, aligned, one line per event. For a terminal.

    The indent is the point: it turns a flat stream into the call tree, which
    is what makes "which tool did this agent actually call" answerable at a
    glance.
    """

    LEVEL_WIDTH = 5

    def __init__(self, colour: Optional[bool] = None):
        super().__init__()
        self.colour = sys.stderr.isatty() if colour is None else colour

    _COLOURS = {
        "TRACE": "\033[38;5;244m",
        "DEBUG": "\033[38;5;110m",
        "INFO": "\033[38;5;255m",
        "WARNING": "\033[38;5;214m",
        "ERROR": "\033[38;5;203m",
        "CRITICAL": "\033[38;5;199m",
    }
    _DIM = "\033[38;5;240m"
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        depth = getattr(record, "depth", 0)
        indent = "  " * depth
        level = record.levelname[: self.LEVEL_WIDTH].ljust(self.LEVEL_WIDTH)
        stamp = self.formatTime(record, "%H:%M:%S") + ".%03d" % record.msecs
        run_id = getattr(record, "run_id", "-")
        name = record.name
        if name.startswith(ROOT_LOGGER_NAME + "."):
            name = name[len(ROOT_LOGGER_NAME) + 1:]

        body = record.getMessage()
        if record.exc_info:
            body = "%s\n%s" % (body, self.formatException(record.exc_info))

        line = "%s %s %s %-22s %s%s" % (stamp, level, run_id, name, indent, body)
        if self.colour:
            tint = self._COLOURS.get(record.levelname, "")
            line = "%s%s %s%s %s%s %-22s%s %s%s%s" % (
                self._DIM, stamp, self._RESET,
                tint + level + self._RESET,
                self._DIM, run_id, name, self._RESET,
                indent, tint + body + self._RESET, "",
            )
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line. For anything that ships logs somewhere."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + ".%03dZ" % record.msecs,
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", "-"),
            "depth": getattr(record, "depth", 0),
            "message": record.getMessage(),
        }
        # Anything a caller attached with `extra=` that is not a LogRecord
        # standard field is part of the event, so it goes in the object.
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key in payload:
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_STANDARD_RECORD_FIELDS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message", "asctime", "taskName",
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def resolve_level(value: Optional[str | int]) -> int:
    """Turns 'DEBUG' / 'debug' / 10 / None into a level int.

    Unknown names fall back to INFO rather than raising, because a typo in an
    env var should not stop the desk from starting.
    """
    if value is None:
        return logging.INFO
    if isinstance(value, int):
        return value
    named = logging.getLevelName(str(value).strip().upper())
    return named if isinstance(named, int) else logging.INFO


def configure(
    level: Optional[str | int] = None,
    fmt: Optional[str] = None,
    stream: Any = None,
    force: bool = False,
) -> logging.Logger:
    """Installs one handler on the `ccb` logger and returns it.

    Args:
      level: level name or int. Defaults to $CCB_LOG_LEVEL, then INFO.
      fmt: "human" (default) or "json". Defaults to $CCB_LOG_FORMAT.
      stream: defaults to stderr, so log output never contaminates a stdout
        payload that something downstream is parsing.
      force: replace an existing handler instead of leaving it alone.

    Calling this twice is safe and does not double-log.
    """
    global _CONFIGURED

    root = logging.getLogger(ROOT_LOGGER_NAME)
    resolved = resolve_level(level if level is not None else os.environ.get("CCB_LOG_LEVEL"))

    if _CONFIGURED and not force:
        root.setLevel(resolved)
        return root

    for handler in list(root.handlers):
        root.removeHandler(handler)

    chosen_format = (fmt or os.environ.get("CCB_LOG_FORMAT") or "human").lower()
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if chosen_format == "json" else HumanFormatter())
    handler.addFilter(_ContextFilter())

    root.addHandler(handler)
    root.setLevel(resolved)
    # The desk owns this subtree; letting records also reach the root logger
    # would double-print them under any app that configured basicConfig.
    root.propagate = False

    _CONFIGURED = True
    return root


def is_configured() -> bool:
    return _CONFIGURED


def get_logger(name: str) -> logging.Logger:
    """A logger under the `ccb` tree, whatever module name you pass.

    `get_logger(__name__)` from `backend/adk_agents.py` yields `ccb.adk_agents`,
    so one `logging.getLogger("ccb").setLevel(...)` moves the whole desk.
    """
    short = name.rsplit(".", 1)[-1] if name != "__main__" else "main"
    return logging.getLogger("%s.%s" % (ROOT_LOGGER_NAME, short))


# A library must not decide where its records go. Without this, a caller that
# never calls configure() gets "No handlers could be found" noise on stderr.
logging.getLogger(ROOT_LOGGER_NAME).addHandler(logging.NullHandler())




# ---------------------------------------------------------------------------
# Trace Buffer
# ---------------------------------------------------------------------------

class TraceBuffer:
    def __init__(self, capacity: int = 50):
        self.capacity = capacity
        self.lock = threading.Lock()
        self.runs: Dict[str, List[Dict[str, Any]]] = {}
        self.kinds: Dict[str, str] = {}
        self.order: List[str] = []
        self.enabled = False

    def enable(self):
        self.enabled = True
        
    def disable(self):
        self.enabled = False
        
    def add_span(self, run_id: str, label: str, depth: int, started_ms: float, elapsed_ms: float, level: int, fields: Dict[str, Any], outcome: Dict[str, Any], state: str, error: Optional[str] = None):
        if not self.enabled or not run_id:
            return
        
        with self.lock:
            if run_id not in self.runs:
                self.runs[run_id] = []
                self.kinds[run_id] = current_run_kind()
                self.order.append(run_id)
                if len(self.order) > self.capacity:
                    oldest = self.order.pop(0)
                    self.runs.pop(oldest, None)
                    self.kinds.pop(oldest, None)
                    
            self.runs[run_id].append({
                "label": label,
                "depth": depth,
                "started_ms": started_ms,
                "elapsed_ms": elapsed_ms,
                "level": level,
                "fields": fields,
                "outcome": outcome,
                "state": state,
                "error": error
            })
            
    def get_trace(self, run_id: str) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self.runs.get(run_id, []))
            
    def get_latest_run_id(self, kind: Optional[str] = None) -> Optional[str]:
        """Most recent run, optionally of one kind.

        Without the filter the UI would happily render a background probe as
        though it were the operator's last turn.
        """
        with self.lock:
            for run_id in reversed(self.order):
                if kind is None or self.kinds.get(run_id) == kind:
                    return run_id
            return None

    def get_run_kind(self, run_id: str) -> str:
        with self.lock:
            return self.kinds.get(run_id, "")

_TRACE_BUFFER = TraceBuffer()

def enable_tracing():
    _TRACE_BUFFER.enable()
    
def disable_tracing():
    _TRACE_BUFFER.disable()
    
def get_trace(run_id: str) -> List[Dict[str, Any]]:
    return _TRACE_BUFFER.get_trace(run_id)

def get_latest_run_id(kind: Optional[str] = None) -> Optional[str]:
    return _TRACE_BUFFER.get_latest_run_id(kind)


def get_run_kind(run_id: str) -> str:
    return _TRACE_BUFFER.get_run_kind(run_id)


_ACTIVE_RUN_ID_FALLBACK: Optional[str] = None
_ACTIVE_RUN_KIND_FALLBACK: str = ""


def current_run_kind() -> str:
    val = _RUN_KIND.get()
    return val if val else _ACTIVE_RUN_KIND_FALLBACK


# ---------------------------------------------------------------------------
# Run correlation
# ---------------------------------------------------------------------------

def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


@contextlib.contextmanager
def run_context(run_id: Optional[str] = None, kind: str = "") -> Iterator[str]:
    """Stamps every record inside the block with one run id, including worker threads spawned by InMemoryRunner."""
    global _ACTIVE_RUN_ID_FALLBACK, _ACTIVE_RUN_KIND_FALLBACK
    rid = run_id or new_run_id()
    prev_id = _ACTIVE_RUN_ID_FALLBACK
    prev_kind = _ACTIVE_RUN_KIND_FALLBACK
    _ACTIVE_RUN_ID_FALLBACK = rid
    _ACTIVE_RUN_KIND_FALLBACK = kind
    token = _RUN_ID.set(rid)
    kind_token = _RUN_KIND.set(kind)
    try:
        yield rid
    finally:
        _RUN_KIND.reset(kind_token)
        _RUN_ID.reset(token)
        _ACTIVE_RUN_ID_FALLBACK = prev_id
        _ACTIVE_RUN_KIND_FALLBACK = prev_kind


def current_run_id() -> str:
    val = _RUN_ID.get()
    if val and val != "-":
        return val
    return _ACTIVE_RUN_ID_FALLBACK or "-"


# ---------------------------------------------------------------------------
# Turn tracing
# ---------------------------------------------------------------------------

class Turn:
    """The handle `turn()` yields. Collects outcome fields for the closing line.

    Fields set with `detail()` are reported once, on exit, so a turn is two
    lines at INFO however much is attached to it.
    """

    __slots__ = ("label", "fields", "outcome", "_logger", "_started")

    def __init__(self, label: str, logger: logging.Logger, fields: Dict[str, Any]):
        self.label = label
        self.fields = dict(fields)
        self.outcome: Dict[str, Any] = {}
        self._logger = logger
        self._started = time.perf_counter()

    def detail(self, **fields: Any) -> "Turn":
        """Attach outcome fields, reported on the closing line."""
        self.outcome.update(fields)
        return self

    def debug_payload(self, message: str, payload: Any) -> None:
        """Log a payload only if DEBUG is on, without paying to render it first.

        The guard matters: serialising a Memory Bank page or a BigQuery result
        set costs real time, and at INFO nobody asked for it.
        """
        if self._logger.isEnabledFor(logging.DEBUG):
            self._logger.debug("%s %s", message, _render(payload))

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._started) * 1000.0


def _render(value: Any, limit: int = 2000) -> str:
    """Compact, truncated, never raises. Logging must not be able to fail a run."""
    try:
        text = json.dumps(value, default=str, separators=(",", ":"))
    except Exception:  # noqa: BLE001 - a repr is always better than an exception here
        text = repr(value)
    return text if len(text) <= limit else "%s... (%d chars truncated)" % (text[:limit], len(text) - limit)


def _fields(mapping: Dict[str, Any]) -> str:
    return " ".join("%s=%s" % (k, _render(v, limit=180)) for k, v in mapping.items())


@contextlib.contextmanager
def turn(
    label: str,
    logger: Optional[logging.Logger] = None,
    level: int = logging.INFO,
    **fields: Any,
) -> Iterator[Turn]:
    """Traces one unit of work: an agent turn, a tool call, a remote call.

    Emits `-> label args` on entry and `<- label ok 123.4ms outcome` on exit, and
    nests, so the indent in the terminal is the actual call tree. On an exception
    it logs `<- label FAILED` at ERROR with the traceback and re-raises: this
    never swallows anything.
    """
    log = logger or logging.getLogger(ROOT_LOGGER_NAME)
    handle = Turn(label, log, fields)

    log.log(level, "-> %s%s", label, (" " + _fields(fields)) if fields else "")

    # The span's own depth is the value *before* we descend. Reading _DEPTH after
    # the reset in the exit paths would yield the parent's depth, not this span's.
    entry_depth = _DEPTH.get()
    depth_token = _DEPTH.set(entry_depth + 1)
    try:
        yield handle
    except Exception as exc:  # noqa: BLE001 - logged and re-raised, not handled
        _DEPTH.reset(depth_token)
        log.error(
            "<- %s FAILED %.1fms %s: %s",
            label, handle.elapsed_ms, type(exc).__name__, exc,
            exc_info=log.isEnabledFor(logging.DEBUG),
        )
        _TRACE_BUFFER.add_span(
            run_id=current_run_id(),
            label=label,
            depth=entry_depth,
            started_ms=handle._started * 1000.0,
            elapsed_ms=handle.elapsed_ms,
            level=level,
            fields=handle.fields,
            outcome=handle.outcome,
            state="error",
            error=f"{type(exc).__name__}: {exc}"
        )
        raise
    else:
        _DEPTH.reset(depth_token)
        log.log(
            level, "<- %s ok %.1fms%s",
            label, handle.elapsed_ms,
            (" " + _fields(handle.outcome)) if handle.outcome else ""
        )
        _TRACE_BUFFER.add_span(
            run_id=current_run_id(),
            label=label,
            depth=entry_depth,
            started_ms=handle._started * 1000.0,
            elapsed_ms=handle.elapsed_ms,
            level=level,
            fields=handle.fields,
            outcome=handle.outcome,
            state="ok",
            error=None
        )



__all__ = [
    "TRACE",
    "HumanFormatter",
    "JsonFormatter",
    "Turn",
    "configure",
    "current_run_id",
    "current_run_kind",
    "get_logger",
    "is_configured",
    "new_run_id",
    "resolve_level",
    "run_context",
    "turn",
]
