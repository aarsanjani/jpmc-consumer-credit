"""
memory_bank.py - Vertex AI Agent Engine Memory Bank over plain REST.

WHAT THIS IS
    A dependency-light client for Memory Bank. It creates sessions, appends
    conversation turns, asks the service to generate memories, and retrieves
    them. Nothing else.

WHAT THIS DELIBERATELY DOES NOT DO
    No local embeddings. No cosine similarity in Python. No "compaction" or
    "dreaming" loop. Those are things the managed service does, and
    reimplementing them on the client would demo our code instead of Google's.

    There is no `ingest_event`. Memory Bank has no ingest operation. Getting a
    fact in means one of exactly two things:

        a) append the turns to a session, then call memories:generate
        b) call memories:generate with directContentsSource and skip sessions

    Both are here. Neither appends to a Python list.

MISSING VALUES
    When the API does not return a field, these helpers render the string
    "n/r" - not recorded. They never substitute a plausible default, a zero,
    or a fabricated id. A zero nobody measured is worse than an obvious blank,
    especially in a governance demo.

API SURFACE USED (all REST, no SDK, no ADK)
    POST   {base}/sessions                        create a session
    POST   {base}/sessions/{id}:appendEvent       add one turn
    GET    {base}/sessions/{id}/events            read the transcript back
    POST   {base}/memories:generate               service extracts the facts
    POST   {base}/memories:retrieve               service does the search
    GET    {base}/memories                        list what is stored

    base = https://{location}-aiplatform.googleapis.com/{version}
           /projects/{project}/locations/{location}/reasoningEngines/{engine}

AUTH
    A bearer token. Sourced, in order: the access_token argument, the
    MEMORY_BANK_TOKEN environment variable, then google.auth default
    credentials if the library is installed. Any language with an HTTP client
    can do the same thing - that is the point of the standalone class below.

Reference:
    https://cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/memory-bank/overview
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

# The string we print when the service did not give us a value. Never a zero,
# never an invented id, never a plausible-looking timestamp.
NOT_RECORDED = "n/r"

DEFAULT_API_VERSION = "v1beta1"   # memories:* live here today
DEFAULT_LOCATION = "us-central1"
DEFAULT_TIMEOUT = 60


class MemoryBankError(RuntimeError):
    """An API call failed. Carries the status and the body, unedited."""

    def __init__(self, status: int, url: str, body: str):
        self.status = status
        self.url = url
        self.body = body
        super().__init__("HTTP %s from %s\n%s" % (status, url, body))


def field(obj: Optional[Dict[str, Any]], *path: str, default: str = NOT_RECORDED) -> Any:
    """Reads a nested field, or returns "n/r".

    The point of this helper is that there is exactly one place in the file
    where a missing value becomes a printable string, and it never invents one.
    """
    node: Any = obj
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    if node is None or node == "" or node == []:
        return default
    return node


def rfc3339(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def require_env(name: str) -> str:
    """A deployment parameter with no safe default.

    Defined here rather than imported from `backend.provenance` so this file
    stays what it claims to be: one module, stdlib only, that you can copy into
    any codebase. Duplicating six lines is the smaller cost.
    """
    raw = os.environ.get(name)
    if not raw:
        raise ValueError(
            "%s is not set. Set it to your own value; this module ships no "
            "default project, engine or location id, because a default is how "
            "a misconfigured run ends up reading another project's data."
            % name
        )
    return raw


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class MemoryBankClient:
    """Memory Bank, spoken to directly over HTTPS.

    Every public method is one API call, named after the operation it makes,
    and returns the decoded response unmodified. If you want a field that the
    service did not send, you get "n/r" - not a guess.
    """

    def __init__(
        self,
        project: str,
        engine_id: str,
        location: str = DEFAULT_LOCATION,
        api_version: str = DEFAULT_API_VERSION,
        access_token: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        verbose: bool = True,
    ):
        if not project or not engine_id:
            raise ValueError(
                "project and engine_id are both required. engine_id is a "
                "parameter on purpose: hardcoding it is what stops a kit from "
                "running in anyone else's project."
            )
        self.project = project
        self.engine_id = engine_id
        self.location = location
        self.api_version = api_version
        self.timeout = timeout
        self.verbose = verbose
        self._token = access_token
        self.calls: List[str] = []   # every request made, for the demo narrative

    # -- plumbing ----------------------------------------------------------

    @property
    def host(self) -> str:
        return "%s-aiplatform.googleapis.com" % self.location

    @property
    def parent(self) -> str:
        return "projects/%s/locations/%s/reasoningEngines/%s" % (
            self.project,
            self.location,
            self.engine_id,
        )

    @property
    def base_url(self) -> str:
        return "https://%s/%s/%s" % (self.host, self.api_version, self.parent)

    def token(self) -> str:
        """Bearer token: explicit arg, then env var, then google.auth."""
        if self._token:
            return self._token
        env = os.environ.get("MEMORY_BANK_TOKEN")
        if env:
            self._token = env
            return env
        try:
            import google.auth
            from google.auth.transport.requests import Request

            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            creds.refresh(Request())
            self._token = creds.token
            return self._token
        except Exception as exc:  # noqa: BLE001 - we want the real reason surfaced
            raise MemoryBankError(
                401,
                "(token)",
                "No credential available. Pass access_token=, set "
                "MEMORY_BANK_TOKEN, or install google-auth and run "
                "`gcloud auth application-default login`. Underlying cause: %s" % exc,
            )

    def _request(self, method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url=url, data=payload, method=method)
        request.add_header("Authorization", "Bearer %s" % self.token())
        request.add_header("Content-Type", "application/json")

        self.calls.append("%s %s" % (method, url.split(self.api_version + "/")[-1]))
        if self.verbose:
            print("  %-6s %s" % (method, url.replace("https://" + self.host, "")))

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise MemoryBankError(exc.code, url, exc.read().decode("utf-8", "replace"))
        except urllib.error.URLError as exc:
            raise MemoryBankError(0, url, "Network error: %s" % exc.reason)

        return json.loads(text) if text.strip() else {}

    def wait_for_operation(self, operation: Dict[str, Any], poll_seconds: float = 1.0,
                           max_wait: int = 120) -> Dict[str, Any]:
        """Blocks until a long-running operation reports done.

        Session creation and memory generation both return an LRO. Appending a
        turn to a session that does not exist yet fails, so this is not
        optional politeness - it is ordering.
        """
        name = operation.get("name")
        if not name or operation.get("done"):
            return operation

        url = "https://%s/%s/%s" % (self.host, self.api_version, name)
        waited = 0.0
        while waited < max_wait:
            time.sleep(poll_seconds)
            waited += poll_seconds
            current = self._request("GET", url)
            if current.get("done"):
                if "error" in current:
                    raise MemoryBankError(0, url, json.dumps(current["error"]))
                return current
        raise MemoryBankError(0, url, "Operation %s still running after %ss." % (name, max_wait))

    # -- sessions ----------------------------------------------------------

    def create_session(self, user_id: str, display_name: Optional[str] = None,
                       session_state: Optional[Dict[str, Any]] = None) -> str:
        """Creates a session and returns its resource name.

        user_id is the memory scope. A session without one would produce
        memories visible to every customer, so it is required here rather than
        defaulted.
        """
        if not user_id:
            raise ValueError("user_id is required - it is what scopes the memories.")

        body: Dict[str, Any] = {"userId": user_id}
        if display_name:
            body["displayName"] = display_name
        if session_state:
            body["sessionState"] = session_state

        operation = self._request("POST", self.base_url + "/sessions", body)
        finished = self.wait_for_operation(operation)
        name = field(finished, "response", "name", default="")
        if not name:
            raise MemoryBankError(0, self.base_url + "/sessions",
                                  "Session created but no name returned: %s" % json.dumps(finished))
        return name

    def append_event(self, session_name: str, author: str, text: str,
                     timestamp: Optional[datetime] = None,
                     invocation_id: Optional[str] = None) -> Dict[str, Any]:
        """Adds one turn to a session.

        The timestamp matters more than it looks. Without it every turn lands
        milliseconds apart at load time, and Memory Bank has no basis to decide
        which of two contradicting statements is the later one.
        """
        event: Dict[str, Any] = {
            "author": author,
            "content": {
                "role": "user" if author == "user" else "model",
                "parts": [{"text": text}],
            },
        }
        if invocation_id:
            event["invocationId"] = invocation_id
        stamp = rfc3339(timestamp)
        if stamp:
            event["timestamp"] = stamp

        url = "https://%s/%s/%s:appendEvent" % (self.host, self.api_version, session_name)
        return self._request("POST", url, event)

    def list_events(self, session_name: str) -> List[Dict[str, Any]]:
        """Reads the transcript back. This is the call the console list view
        does not make, which is why a seeded session can look empty there."""
        url = "https://%s/%s/%s/events" % (self.host, self.api_version, session_name)
        return self._request("GET", url).get("sessionEvents", [])

    def list_sessions(self) -> List[Dict[str, Any]]:
        return self._request("GET", self.base_url + "/sessions").get("sessions", [])

    # -- memories ----------------------------------------------------------

    def generate_from_session(self, session_name: str, scope: Dict[str, str],
                              ignore_consolidation: bool = False,
                              wait: bool = True) -> Dict[str, Any]:
        """Asks the service to extract memories from a session.

        This is where Gemini runs, and it runs inside Google, not here. We send
        two things: which session to read and which bucket to write into. We do
        not send a prompt, choose a model, or see the tokens.
        """
        body: Dict[str, Any] = {
            "vertexSessionSource": {"session": session_name},
            "scope": scope,
        }
        if ignore_consolidation:
            body["ignoreConsolidation"] = True

        operation = self._request("POST", self.base_url + "/memories:generate", body)
        return self.wait_for_operation(operation) if wait else operation

    def generate_from_contents(self, turns: Iterable[Dict[str, str]], scope: Dict[str, str],
                               ignore_consolidation: bool = False,
                               wait: bool = True) -> Dict[str, Any]:
        """Extracts memories from raw turns, with no session involved.

        This is the path for a non-ADK agent that keeps its own conversation
        history and only wants the memory layer. Nothing has to be stored as a
        Vertex session first.
        """
        contents = [
            {
                "role": "user" if t.get("role") == "user" else "model",
                "parts": [{"text": t["text"]}],
            }
            for t in turns
        ]
        body: Dict[str, Any] = {
            "directContentsSource": {"events": [{"content": c} for c in contents]},
            "scope": scope,
        }
        if ignore_consolidation:
            body["ignoreConsolidation"] = True

        operation = self._request("POST", self.base_url + "/memories:generate", body)
        return self.wait_for_operation(operation) if wait else operation

    def retrieve(self, scope: Dict[str, str], query: Optional[str] = None,
                 top_k: int = 5, memory_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves memories for a scope.

        With a query it is a semantic search run by the service. Without one it
        returns everything in scope. Either way the ranking happens server side
        - there is no vector maths in this file.
        """
        body: Dict[str, Any] = {"scope": scope}
        if query:
            body["similaritySearchParams"] = {"searchQuery": query, "topK": top_k}
        else:
            body["simpleRetrievalParams"] = {"pageSize": top_k}
        if memory_filter:
            body["filter"] = memory_filter

        response = self._request("POST", self.base_url + "/memories:retrieve", body)
        return response.get("retrievedMemories", [])

    def list_memories(self) -> List[Dict[str, Any]]:
        return self._request("GET", self.base_url + "/memories").get("memories", [])

    def create_memory(self, fact: str, scope: Dict[str, str]) -> Dict[str, Any]:
        """Writes one already-extracted fact directly, bypassing generation.

        memories:generate is the right door when you have a conversation and
        want the service to decide what is worth remembering. Seeding is the
        other case: the fact is already atomic and the operator, not a model,
        decides it belongs. Going through generate for a known fact would let
        the extractor re-word or merge it, which is exactly what produced the
        narrative blob this seeding path replaces.

        scope arrives per call so one engine-level client can serve every
        customer; binding it to the constructor is how memories leak between
        customers.
        """
        if not fact or not fact.strip():
            raise ValueError("fact is required - an empty memory is not a memory.")
        if not scope:
            raise ValueError("scope is required - an unscoped memory is visible to every customer.")

        return self._request("POST", self.base_url + "/memories",
                             {"fact": fact, "scope": scope})

    def delete_memory(self, memory_name: str) -> Dict[str, Any]:
        """Deletes one memory by full resource name.

        Takes the name the service returned rather than an id fragment, so a
        caller cannot delete in the wrong engine by pasting a bare id.
        """
        if not memory_name or "/memories/" not in memory_name:
            raise ValueError(
                "memory_name must be the full resource name the service returned, "
                "e.g. projects/../locations/../reasoningEngines/../memories/.., "
                "not a bare id."
            )
        return self._request("DELETE", "https://%s/%s/%s" % (self.host, self.api_version, memory_name))


# ---------------------------------------------------------------------------
# Rendering - the only place values become strings
# ---------------------------------------------------------------------------


def memory_line(item: Dict[str, Any]) -> str:
    """One retrieved memory as a printable line.

    Every field is read through field(), so anything the service omitted shows
    as n/r instead of a default we made up.
    """
    memory = item.get("memory", item)
    fact = field(memory, "fact")
    updated = field(memory, "updateTime")
    distance = field(item, "distance")
    topics = field(memory, "topics")
    if isinstance(topics, list):
        topics = ", ".join(str(t) for t in topics)
    return "  - %s\n      updated %s | distance %s | topics %s" % (
        fact, updated, distance, topics,
    )


# ---------------------------------------------------------------------------
# Using Memory Bank from something that is not an ADK agent
# ---------------------------------------------------------------------------


class StandaloneMemoryBankClient:
    """Memory Bank for a caller that is not an ADK agent.

    The point this class exists to prove: nothing about Memory Bank requires
    ADK, the Vertex SDK, Python, or even Google Cloud as the place your agent
    runs. It is three HTTP calls behind a bearer token. A LangGraph graph, a
    CrewAI crew, a Java service on AWS or a Lambda can all use it the same way.

    So this class holds no logic of its own. It calls MemoryBankClient, and it
    reports the literal HTTP requests it made so a reviewer can see there is
    nothing else going on.

    The integration is:

        1. before your agent reasons   -> memories:retrieve, put the facts in
                                          your prompt however your framework
                                          does that
        2. after the conversation ends -> memories:generate, with either a
                                          session or the raw turns

    That is the whole contract.
    """

    def __init__(self, client: Optional[MemoryBankClient] = None,
                 customer_id: str = "CUST00001",
                 framework_name: str = "LangGraph"):
        self.client = client or create_default_memory_bank_client()
        self.customer_id = customer_id
        self.framework_name = framework_name

    @property
    def scope(self) -> Dict[str, str]:
        return {"customer_id": self.customer_id}

    def preload_context_for_external_agent(self, user_query: str, top_k: int = 5) -> Dict[str, Any]:
        """Step 1: fetch the facts, hand them back as plain data.

        Returns a dict, not an object. Whatever framework you are in can drop
        `prompt_block` straight into a system message.
        """
        before = len(self.client.calls)
        memories = self.client.retrieve(self.scope, query=user_query, top_k=top_k)

        facts = [field(m.get("memory", m), "fact") for m in memories]
        known = [f for f in facts if f != NOT_RECORDED]

        prompt_block = (
            "What you already know about this customer:\n"
            + "\n".join("- %s" % f for f in known)
        ) if known else "No stored memories for this customer."

        return {
            "framework": self.framework_name,
            "scope": self.scope,
            "query": user_query,
            "memory_count": len(memories),
            "facts": facts or [NOT_RECORDED],
            "prompt_block": prompt_block,
            # Proof of what actually crossed the wire.
            "http_calls": self.client.calls[before:],
            "sdk_used": "none - urllib over HTTPS",
            "adk_used": False,
        }

    def persist_turns_after_run(self, turns: List[Dict[str, str]]) -> Dict[str, Any]:
        """Step 2: hand the conversation back for extraction.

        Uses directContentsSource, so the external framework keeps owning its
        own conversation history and never has to store a Vertex session.
        """
        before = len(self.client.calls)
        operation = self.client.generate_from_contents(turns, self.scope)
        return {
            "framework": self.framework_name,
            "turns_sent": len(turns),
            "operation": field(operation, "name"),
            "done": operation.get("done", False),
            "http_calls": self.client.calls[before:],
            "note": "Extraction is asynchronous. Retrieve again shortly to see the result.",
        }


# ---------------------------------------------------------------------------
# The Lisbon scenario
# ---------------------------------------------------------------------------

# 09 Sep 2026, IVR. The customer is in Lisbon, on a trip the bank already knows
# about, and reports the wallet gone. Nothing here states a dollar amount or a
# regulatory date on purpose: those come from BigQuery, never from a memory.
LISBON_THEFT_TURNS: List[Dict[str, Any]] = [
    {"role": "user",
     "text": "My wallet was stolen this morning. I am in Lisbon. Both my cards were in it.",
     "time": datetime(2026, 9, 9, 8, 14, tzinfo=timezone.utc)},
    {"role": "agent",
     "text": "I am sorry. I can see the travel notice for Lisbon through the twelfth. "
             "Which cards were in the wallet?",
     "time": datetime(2026, 9, 9, 8, 15, tzinfo=timezone.utc)},
    {"role": "user",
     "text": "The debit card and the credit card. Both of them. Freeze them now.",
     "time": datetime(2026, 9, 9, 8, 16, tzinfo=timezone.utc)},
    {"role": "agent",
     "text": "Both are frozen. I have also revoked the wallet tokens on your phone.",
     "time": datetime(2026, 9, 9, 8, 18, tzinfo=timezone.utc)},
    {"role": "user",
     "text": "I am still here until the twelfth, so I need a card that works. "
             "And do not close anything without telling me first.",
     "time": datetime(2026, 9, 9, 8, 20, tzinfo=timezone.utc)},
    {"role": "agent",
     "text": "Understood. A virtual card is available immediately and the replacement "
             "goes to your hotel. Nothing will be closed without contacting you.",
     "time": datetime(2026, 9, 9, 8, 22, tzinfo=timezone.utc)},
]


def run_lisbon_contradiction_demo(client: MemoryBankClient, customer_id: str = "CUST00001",
                                  use_session: bool = True) -> Dict[str, Any]:
    """The demo beat: a new fact inverts the meaning of an old one.

    Before the theft, the travel notice is the reason Lisbon spend is NOT
    fraud. After it, the same memory makes Lisbon spend the most suspicious
    spend on the account - the customer is there and the cards are not.

    That is what the memory layer buys you, and it is not something a
    retrieval-over-transcripts system does. It is also why this runs as
    retrieve -> append -> generate -> retrieve, so the flip is visible rather
    than asserted.
    """
    scope = {"customer_id": customer_id}
    result: Dict[str, Any] = {"customer_id": customer_id, "steps": []}

    print("\n" + "=" * 72)
    print("LISBON CONTRADICTION FLOW - customer %s" % customer_id)
    print("=" * 72)

    # 1. What does the bank believe right now?
    print("\n[1] Before the theft. Asking what we know about travel.")
    before = client.retrieve(scope, query="Where is this customer travelling and when?")
    for item in before:
        print(memory_line(item))
    if not before:
        print("  (no memories in scope - seed the corpus first)")
    result["steps"].append({"step": "retrieve_before", "count": len(before),
                            "facts": [field(m.get("memory", m), "fact") for m in before]})

    # 2. The new event arrives.
    print("\n[2] The theft call comes in. Six turns, with their real timestamps.")
    if use_session:
        session = client.create_session(
            user_id=customer_id,
            display_name="IVR THEFT-2026-09-09",
            session_state={"channel": "IVR", "customer_id": customer_id,
                           "intent": "LOST_STOLEN", "source": "jpmc-ccb-fraud-demo-kit"},
        )
        print("      session: %s" % session.split("/")[-1])
        for index, turn in enumerate(LISBON_THEFT_TURNS, start=1):
            client.append_event(
                session_name=session,
                author="user" if turn["role"] == "user" else "fraud-desk",
                text=turn["text"],
                timestamp=turn["time"],
                invocation_id="THEFT-%03d" % index,
            )
        print("      appended %d turns" % len(LISBON_THEFT_TURNS))
        result["steps"].append({"step": "append", "session": session,
                                "turns": len(LISBON_THEFT_TURNS)})

        # 3. Ask the service to extract. Gemini runs inside Google, not here.
        print("\n[3] Asking Memory Bank to generate. Consolidation happens server side.")
        operation = client.generate_from_session(session, scope)
    else:
        print("      (no session - sending the turns directly)")
        print("\n[3] Asking Memory Bank to generate from raw contents.")
        operation = client.generate_from_contents(LISBON_THEFT_TURNS, scope)

    print("      operation: %s" % field(operation, "name"))
    result["steps"].append({"step": "generate", "operation": field(operation, "name"),
                            "done": operation.get("done", False)})

    # 4. Ask the same question again.
    print("\n[4] After. Same question, and the answer should now mean something else.")
    after = client.retrieve(scope, query="Where is this customer travelling and when?")
    for item in after:
        print(memory_line(item))
    result["steps"].append({"step": "retrieve_after", "count": len(after),
                            "facts": [field(m.get("memory", m), "fact") for m in after]})

    # 5. The second kind of value: a memory that constrains what the agent may do.
    print("\n[5] The standing instruction, which is a different kind of memory.")
    instructions = client.retrieve(
        scope, query="What has the customer told us we must always do?")
    for item in instructions:
        print(memory_line(item))
    result["steps"].append({"step": "retrieve_instructions", "count": len(instructions),
                            "facts": [field(m.get("memory", m), "fact") for m in instructions]})

    print("""
WHAT TO SAY WHILE THIS RUNS
  The travel notice did not change. Its meaning did. Before the call it is the
  reason Lisbon spend is expected; after it, it is the reason Lisbon spend is
  the most suspicious spend on the account.

  Two memories, two different jobs. The travel fact changes what the agent
  CONCLUDES. The standing instruction changes what the agent is ALLOWED to do,
  and it is what produces the refusal in the actions trace.

  Note what is absent: no amount, no deadline, no dispute id. Those come from
  BigQuery. A memory supplies context; it never supplies a fact you would put
  in a regulatory filing.
""")
    return result


# ---------------------------------------------------------------------------
# Engine-level Factory Helper
# ---------------------------------------------------------------------------


def create_default_memory_bank_client(verbose: bool = False) -> MemoryBankClient:
    """Creates a service-level MemoryBankClient from environment configuration.

    NOTE: MemoryBankClient is initialized at the ENGINE level (project, engine_id,
    location) — never bound to a single `customer_id`. Customer scope is passed
    per API request (`scope={"customer_id": customer_id}`).
    """
    # No literal fallback here either. A default project id is the same defect
    # as a default engine id: the run looks healthy and reads somebody else's
    # data.
    project = require_env("GOOGLE_CLOUD_PROJECT")
    engine_id = (
        os.environ.get("VERTEX_AGENT_ENGINE_ID")
        or os.environ.get("AGENT_ENGINE_ID")
        or os.environ.get("VERTEX_MEMORY_BANK_ENGINE_ID")
    )
    # No literal fallback: MemoryBankClient raises when engine_id is missing,
    # which is better than silently running against a hardcoded engine.
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", DEFAULT_LOCATION)
    return MemoryBankClient(
        project=project,
        engine_id=engine_id,
        location=location,
        verbose=verbose,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Vertex AI Agent Engine Memory Bank over plain REST.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples
  # what is stored for one customer
  python memory_bank.py --project P --engine-id E list --customer CUST00001

  # read a seeded session back, which the console list view will not show you
  python memory_bank.py --project P --engine-id E events --session-id 2183930568675164160

  # the demo
  python memory_bank.py --project P --engine-id E lisbon --customer CUST00001

  # prove a non-ADK framework can use it
  python memory_bank.py --project P --engine-id E standalone \\
      --customer CUST00001 --query "is this customer travelling?"
""",
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--engine-id", required=True,
                        help="Reasoning engine id. A parameter, never hardcoded.")
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    parser.add_argument("--access-token", default=None,
                        help="Bearer token. Falls back to MEMORY_BANK_TOKEN, then google.auth.")
    parser.add_argument("--quiet", action="store_true", help="Do not echo each HTTP call.")

    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Retrieve memories for a customer.")
    p_list.add_argument("--customer", required=True)
    p_list.add_argument("--query", default=None,
                        help="Semantic search. Omit to return everything in scope.")
    p_list.add_argument("--top-k", type=int, default=10)

    p_events = sub.add_parser("events", help="Read a session transcript back.")
    p_events.add_argument("--session-id", required=True)

    p_sessions = sub.add_parser("sessions", help="List sessions on the engine.")

    p_lisbon = sub.add_parser("lisbon", help="Run the contradiction flow.")
    p_lisbon.add_argument("--customer", default="CUST00001")
    p_lisbon.add_argument("--direct", action="store_true",
                          help="Skip sessions, use directContentsSource.")

    p_alone = sub.add_parser("standalone", help="Show the non-ADK integration.")
    p_alone.add_argument("--customer", required=True)
    p_alone.add_argument("--query", default="What should I know about this customer?")
    p_alone.add_argument("--framework", default="LangGraph")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    client = MemoryBankClient(
        project=args.project,
        engine_id=args.engine_id,
        location=args.location,
        api_version=args.api_version,
        access_token=args.access_token,
        verbose=not args.quiet,
    )

    try:
        if args.command == "list":
            scope = {"customer_id": args.customer}
            memories = client.retrieve(scope, query=args.query, top_k=args.top_k)
            print("\n%d memory(ies) in scope %s" % (len(memories), scope))
            for item in memories:
                print(memory_line(item))
            if not memories:
                print("  (none - either nothing was seeded, or generation is still running)")

        elif args.command == "events":
            session_name = "%s/sessions/%s" % (client.parent, args.session_id)
            events = client.list_events(session_name)
            print("\n%d event(s) in session %s" % (len(events), args.session_id))
            for event in events:
                parts = field(event, "content", "parts", default=[])
                text = parts[0].get("text", NOT_RECORDED) if isinstance(parts, list) and parts else NOT_RECORDED
                print("  [%s] %-12s %s" % (field(event, "timestamp"), field(event, "author"), text))
            if not events:
                print("  (none - if you seeded this session, generation order or an "
                      "appendEvent failure is the place to look)")

        elif args.command == "sessions":
            sessions = client.list_sessions()
            print("\n%d session(s)" % len(sessions))
            for session in sessions:
                print("  %-22s %-24s %s" % (
                    field(session, "name", default=NOT_RECORDED).split("/")[-1],
                    field(session, "displayName"),
                    field(session, "userId"),
                ))

        elif args.command == "lisbon":
            run_lisbon_contradiction_demo(client, args.customer, use_session=not args.direct)

        elif args.command == "standalone":
            bridge = StandaloneMemoryBankClient(client, args.customer, args.framework)
            context = bridge.preload_context_for_external_agent(args.query)
            print("\n--- what a %s agent gets back ---" % context["framework"])
            print(context["prompt_block"])
            print("\nHTTP calls made: %s" % (context["http_calls"] or [NOT_RECORDED]))
            print("SDK used: %s | ADK used: %s" % (context["sdk_used"], context["adk_used"]))

    except MemoryBankError as error:
        print("\nAPI call failed.\n%s" % error, file=sys.stderr)
        return 1
    except ValueError as error:
        print("\n%s" % error, file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
