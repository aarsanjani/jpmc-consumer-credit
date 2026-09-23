"""
Synthesizer agent for the CCB Fraud Desk.

Its one job is to turn what the bank already knows into an answer the customer
does not have to be interrogated for. It reads memories from Vertex AI Memory
Bank and rows from the parameterised BigQuery golden queries, asks Gemini to
join them, and streams the result as JSON-RPC 2.0 trace frames.

Two rules shape the code below:

  - If the model call fails, the failure is what comes back. There is no
    rehearsed narrative standing by to make a broken run look like a good one.
  - The causal chain is built from retrieved records, so it is as long as the
    evidence is. No evidence produces one step that says so, not three invented
    ones.
"""

import json
import logging
import os
from typing import Any, Dict, Generator, List, Optional

import certifi
from dotenv import load_dotenv
from google import genai

from backend.models import BankChannel, CausalStep, SynthesisResult
from backend.memory_bank import MemoryBankClient, MemoryBankError, field as mb_field
from backend.provenance import NOT_RECORDED, computed, identity, live, require_env, unmeasured

load_dotenv()

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
)

AGENT_NAME = "ConsumerCreditSynthesizerAgent"
AGENT_SOURCE = "backend/agent.py:AGENT_NAME"

# The mandated containment order. A policy constant the behaviour layer grades
# against, not something this process observed.
CONTAINMENT_SEQUENCE = (
    "freeze_card",
    "revoke_wallet_token",
    "issue_virtual_card",
    "order_replacement_card",
)

SYSTEM_INSTRUCTION = (
    "You are the Consumer Credit and Fraud Desk synthesizer for a retail bank.\n"
    "You are given two things: memories retrieved from this customer's Memory "
    "Bank, and rows returned by governed BigQuery queries.\n\n"
    "Rules:\n"
    "1. Ask nothing. Everything the customer told any channel is in the memories "
    "below; re-asking is the failure this desk exists to remove.\n"
    "2. Use only the supplied memories and rows. If a figure, date or identifier "
    "is not in them, write n/r. Never estimate one.\n"
    "3. Debit and credit disputes are governed by different rules. Name the rule "
    "per card, and only when the supplied rows name that card and dispute.\n"
    "4. When two facts conflict, say which is newer and why it wins."
)


class MemoryBankSynthesizerAgent:
    """Gemini on Vertex AI, grounded in Memory Bank plus the golden queries."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        location: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        self.project_id = project_id or require_env("GOOGLE_CLOUD_PROJECT")
        self.location = location or os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        self.model_name = model_name or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self._client: Optional[genai.Client] = None

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
            self._client = genai.Client(
                vertexai=True, project=self.project_id, location=self.location
            )
        return self._client

    # -- grounding ---------------------------------------------------------

    @staticmethod
    def _memory_records(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        records = []
        for item in items:
            body = item.get("memory", item)
            name = mb_field(body, "name")
            records.append(
                {
                    "memory_id": name.split("/")[-1] if name != NOT_RECORDED else NOT_RECORDED,
                    "fact": mb_field(body, "fact"),
                    "update_time": mb_field(body, "updateTime"),
                    "distance": item.get("distance", NOT_RECORDED),
                }
            )
        return records

    @staticmethod
    def _golden(tool_name: str, **kwargs) -> Dict[str, Any]:
        """Calls one golden-query tool. A failure comes back as data, not a raise."""
        try:
            from backend import adk_agents
        except Exception as exc:  # noqa: BLE001 - the import chain is the story
            return {"allowed": False, "error": "backend.adk_agents unavailable: %s" % exc}
        fn = getattr(adk_agents, tool_name, None)
        if fn is None:
            return {"allowed": False, "error": "no tool named %s" % tool_name}
        try:
            return fn(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logging.error("Golden query %s failed: %s", tool_name, exc)
            return {"allowed": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    def gather_grounding(
        self, customer_id: str, user_prompt: str, memory_client: MemoryBankClient
    ) -> Dict[str, Any]:
        """Everything the answer is allowed to be built from, plus its origin."""
        try:
            retrieved = memory_client.retrieve(
                {"customer_id": customer_id}, query=user_prompt or None, top_k=10
            )
            memories = self._memory_records(retrieved)
            memory_error = None
        except MemoryBankError as exc:
            memories = []
            memory_error = str(exc)

        return {
            "customer_id": customer_id,
            "memories": memories,
            "memory_error": memory_error,
            "open_disputes": self._golden(
                "golden_07_open_disputes_for_customer", customer_id=customer_id
            ),
            "customer_360": self._golden(
                "golden_01_customer_360_snapshot", customer_id=customer_id
            ),
        }

    # -- synthesis ---------------------------------------------------------

    def _call_genai_synthesis(self, prompt: str, grounding: Dict[str, Any]) -> Dict[str, Any]:
        """Returns the narrative, or the reason there is not one. Never both."""
        contents = (
            "MEMORIES RETRIEVED FROM MEMORY BANK:\n"
            + json.dumps(grounding["memories"], indent=2, default=str)
            + "\n\nGOVERNED QUERY ROWS:\n"
            + json.dumps(
                {
                    "open_disputes": grounding["open_disputes"],
                    "customer_360": grounding["customer_360"],
                },
                indent=2,
                default=str,
            )
            + "\n\nCUSTOMER MESSAGE: %r\n\n" % prompt
            + "Answer from the material above. Ask no questions."
        )
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=dict(system_instruction=SYSTEM_INSTRUCTION, temperature=0.2),
            )
        except Exception as exc:  # noqa: BLE001 - a broken run must read as broken
            logging.error("Gemini synthesis failed: %s", exc)
            return {"text": None, "reason": "vertexai.generateContent failed: %s" % exc}

        text = (response.text or "").strip()
        if not text:
            return {"text": None, "reason": "vertexai.generateContent returned no text"}
        return {"text": text, "reason": None}

    def _causal_chain(
        self, grounding: Dict[str, Any], request_channel: BankChannel
    ) -> List[CausalStep]:
        """One step per record actually retrieved.

        `channel` on each step is the channel the request arrived on, not the
        channel that produced the memory: retrieval does not return that, and
        `CausalStep.channel` is a required enum with no unknown member.
        """
        steps: List[CausalStep] = []

        for record in grounding["memories"]:
            steps.append(
                CausalStep(
                    step_number=len(steps) + 1,
                    timestamp=record["update_time"],
                    channel=request_channel,
                    event_title=record["fact"],
                    causal_explanation=(
                        "Memory %s, retrieved at distance %s. Channel shown is the "
                        "requesting channel; the originating channel is not recorded."
                        % (record["memory_id"], record["distance"])
                    ),
                    linked_fragment_id=record["memory_id"],
                )
            )

        disputes = grounding["open_disputes"]
        rows = disputes.get("rows", []) if isinstance(disputes, dict) else []
        for row in rows:
            steps.append(
                CausalStep(
                    step_number=len(steps) + 1,
                    timestamp=str(row.get("opened_at", NOT_RECORDED)),
                    channel=BankChannel.CORE_BANKING,
                    event_title=str(row.get("dispute_id", NOT_RECORDED)),
                    causal_explanation=json.dumps(row, default=str),
                    linked_fragment_id=str(row.get("dispute_id", NOT_RECORDED)),
                )
            )

        if not steps:
            steps.append(
                CausalStep(
                    step_number=1,
                    timestamp=NOT_RECORDED,
                    channel=request_channel,
                    event_title="No evidence returned",
                    causal_explanation=(
                        grounding.get("memory_error")
                        or "Memory Bank and the dispute query both returned zero rows."
                    ),
                    linked_fragment_id=NOT_RECORDED,
                )
            )
        return steps

    def synthesize_customer_issue(
        self,
        customer_id: str,
        user_prompt: str,
        memory_client: MemoryBankClient,
        channel: BankChannel = BankChannel.WEB_PORTAL,
    ) -> SynthesisResult:
        grounding = self.gather_grounding(customer_id, user_prompt, memory_client)
        narrative = self._call_genai_synthesis(user_prompt, grounding)

        return SynthesisResult(
            customer_id=customer_id,
            executive_summary=narrative["text"] or NOT_RECORDED,
            # Nothing in this process measures a root cause independently of the
            # narrative, so the field does not restate one as if it had.
            root_cause_identified=NOT_RECORDED,
            causal_chain=self._causal_chain(grounding, channel),
            recommended_actions=list(CONTAINMENT_SEQUENCE),
            applicable_policies=[],
            # `SynthesisResult.confidence_score` is a required float in the shared
            # models, so it cannot carry "n/r". The honest value is in the A2UI
            # payload; treat this field as unset, not as a score of zero.
            confidence_score=0.0,
            zero_question_greeting=(
                "I already have your case from the other channels, so there is "
                "nothing to re-ask."
            ),
        )

    def a2ui_payload(
        self, customer_id: str, grounding: Dict[str, Any], result: SynthesisResult
    ) -> Dict[str, Any]:
        """The synthesis card. Every field either proves its source or reads n/r."""
        has_narrative = result.executive_summary != NOT_RECORDED
        return {
            "type": "MemorySynthesisCard",
            "title": "Cross-system Memory Bank and Knowledge Catalog synthesis",
            "customer_id": computed(customer_id, "request customer_id"),
            "memory_count": computed(
                len(grounding["memories"]), "reasoningEngines.memories:retrieve"
            ),
            "narrative": live(
                result.executive_summary, "vertexai.generateContent:%s" % self.model_name
            )
            if has_narrative
            else unmeasured("vertexai.generateContent produced no narrative"),
            "root_cause": unmeasured("not measured independently of the narrative"),
            "confidence_score": unmeasured("no calibrated confidence is computed for synthesis"),
            "timeline": [
                {
                    "timestamp": step.timestamp,
                    "channel": step.channel.value,
                    "title": step.event_title,
                    "detail": step.causal_explanation,
                    "linked_fragment_id": step.linked_fragment_id,
                }
                for step in result.causal_chain
            ],
            "recommended_actions": [
                identity(action, "backend/agent.py:CONTAINMENT_SEQUENCE", "mandated containment order")
                for action in result.recommended_actions
            ],
            "resolution": {
                "action_id": "ADMIN_CARD_DECISION",
                "label": "Send to the supervisor for the unlock decision",
                "description": "Routes the case to /api/card-oversight/decide for a YES or NO.",
            },
        }

    # -- streaming ---------------------------------------------------------

    def stream_synthesis_workflow(
        self,
        session_id: str,
        customer_id: str,
        user_prompt: str,
        memory_client: MemoryBankClient,
        channel: BankChannel = BankChannel.WEB_PORTAL,
    ) -> Generator[str, None, None]:
        """Yields the four A2UI frames the frontend subscribes to, in order.

        Frame names are a fixed contract: onAgentThought, onMemoryBankAccess,
        onAgentSynthesis, onUiComponentDelivery.
        """
        logging.info("Session %s: synthesis for %s", session_id, customer_id)
        author = identity(AGENT_NAME, AGENT_SOURCE, "the agent's own name")

        yield self._encode_rpc_frame(
            "onAgentThought",
            {
                "author": author,
                "message": "Reading this customer's memories and open disputes before answering.",
            },
        )

        grounding = self.gather_grounding(customer_id, user_prompt, memory_client)

        yield self._encode_rpc_frame(
            "onMemoryBankAccess",
            {
                "author": identity("MemoryBankClient", "backend/memory_bank.py", "the client's own name"),
                "customer_id": computed(customer_id, "request customer_id"),
                "memory_count": computed(
                    len(grounding["memories"]), "reasoningEngines.memories:retrieve"
                ),
                "memories": live(grounding["memories"], "reasoningEngines.memories:retrieve")
                if grounding["memory_error"] is None
                else unmeasured(
                    "reasoningEngines.memories:retrieve (%s)" % grounding["memory_error"]
                ),
            },
        )

        yield self._encode_rpc_frame(
            "onAgentThought",
            {
                "author": author,
                "message": "Joining memories to the governed dispute rows. No clarifying question needed.",
            },
        )

        result = self.synthesize_customer_issue(customer_id, user_prompt, memory_client, channel)

        yield self._encode_rpc_frame(
            "onAgentSynthesis",
            {
                "author": author,
                "customer_id": computed(customer_id, "request customer_id"),
                "narrative": live(
                    result.executive_summary, "vertexai.generateContent:%s" % self.model_name
                )
                if result.executive_summary != NOT_RECORDED
                else unmeasured("vertexai.generateContent produced no narrative"),
                "causal_steps": [s.model_dump(mode="json") for s in result.causal_chain],
                "asked_question": computed(False, "the desk does not emit clarifying questions"),
            },
        )

        yield self._encode_rpc_frame(
            "onUiComponentDelivery",
            {
                "author": author,
                "ui_specification": "A2UI-0.9",
                "payload": self.a2ui_payload(customer_id, grounding, result),
            },
        )

    @staticmethod
    def _encode_rpc_frame(method: str, params: Dict[str, Any]) -> str:
        return json.dumps({"jsonrpc": "2.0", "method": method, "params": params}, default=str)
