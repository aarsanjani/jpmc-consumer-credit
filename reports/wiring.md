# Wiring, imports and provenance — worker report

Scope: I own and delivered `backend/app.py`, `backend/agent.py`,
`frontend/static/js/app.js`, and one new file `backend/custom_agent_comparison.py`.
Everything else named below is somebody else's file and is reported, not edited.

**Verification status, stated plainly.** No flask, no pydantic, no pytest, no
network here, so I could not import the backend, start the server or run
`tests/test_backend.py`. What I *did* verify is listed in the last section. Every
backend behaviour described below is **written but unrun**.

---

## 1. Problem A — the import failures

### What was actually broken

| # | Symptom | Root cause |
|---|---|---|
| 1 | `app.py` ~L40 imports `CustomerMemoryBank`, `GroundTruthTelemetryStore`, `StandaloneMemoryBankClient` from `backend.memory_bank` | Only `StandaloneMemoryBankClient` exists. The other two are re-exported at `memory_bank.py:630` from **`backend.memory_bank_old`, which is not in this repo**, inside `try/except Exception: pass`. The swallow is why this never surfaced as an import error — it surfaced as `NameError` at request time instead. |
| 2 | `app.py` ~L44 imports `KnowledgeCatalog`, `AWSOnPremHybridConnector` from `backend.knowledge_catalog` | Same pattern at `knowledge_catalog.py:586`, re-exported from the non-existent `backend.knowledge_catalog_old`. |
| 3 | `app.py` ~L46 `from backend.custom_agent_comparison import AgentComparisonEngine` | Module does not exist at all. This one is a hard `ModuleNotFoundError` — it is the reason the app cannot start. |
| 4 | `agent.py` ~L15 imports `GroundTruthTelemetryStore` and `KnowledgeCatalog` | Same two phantom symbols. |

### How I fixed them

Nothing `_old` is imported any more. Every call now goes through the real REST
clients:

| Old phantom symbol | Replaced by |
|---|---|
| `CustomerMemoryBank(customer_id=...)` | `MemoryBankClient(project, engine_id, location)` — engine-level, customer passed per call as `scope={"customer_id": ...}` |
| `mb.ingest_event(...)` | `client.generate_from_contents(turns, scope)` — extraction runs in the service |
| `mb.retrieve_relevant_memories(query, top_k)` | `client.retrieve(scope, query, top_k)` |
| `mb.get_fragments()` | `client.retrieve(scope)` |
| `mb.seed_default_scenario()` | `client.generate_from_contents(LISBON_THEFT_TURNS, scope)` |
| `mb.compact_memories()` | `client.generate_from_session(...)` — see §4 |
| `CustomerMemoryBank.open_channel_session(...)` | `client.create_session(user_id, display_name, session_state)` |
| `CustomerMemoryBank.list_sessions()` | `client.list_sessions()` |
| `GroundTruthTelemetryStore.get_telemetry_for_customer` | golden queries `01/02/03/04/07/10` via `execute_golden_query_contract` |
| `GroundTruthTelemetryStore.fetch_live_account_statement` | golden queries `01/02/07/09` |
| `KnowledgeCatalog.query_bigquery_customer_360` | `golden_01_customer_360_snapshot` |
| `KnowledgeCatalog.get_dataplex_catalog_governance` | `CatalogGovernor.check_table()` / `check_column()` |
| `KnowledgeCatalog.get_all_policies` | `DataplexCatalogClient.list_terms()` |
| `KnowledgeCatalog.get_customer_risk_profile` | `golden_01_customer_360_snapshot` |
| `KnowledgeCatalog.get_entity_subgraph` | **no real producer → `"n/r"`** |
| `AWSOnPremHybridConnector.get_connector_topology` | **no real producer → `"n/r"`** |
| `KnowledgeCatalog._clean_iso_records` | dropped; the REST clients already return JSON-safe values |

### The one that is NOT fixed, and cannot be by me

`backend/veracity_and_audit.py:37-38` still does:

```python
from backend.memory_bank import CustomerMemoryBank, GroundTruthTelemetryStore
from backend.knowledge_catalog import KnowledgeCatalog
```

That is the same phantom-symbol bug, in a file I do not own. It matters more
than it looks, because `backend/adk_agents.py:49` imports
`veracity_and_audit`, so **both** of those modules fail to import for the same
reason. Between them they supply the golden queries, the agent roster, the
claim validator, the audit sweep and the admin oversight manager.

I did not silently route around it. `app.py` imports both modules through a
recorded loader: the exact `ImportError` text is kept, logged at `ERROR` on
boot, and returned by the affected routes as **HTTP 503** naming the module and
the error. No route disappears, no route invents a payload.

**Action required from the owner of `veracity_and_audit.py`:** point those three
imports at the real clients. Until then, 13 of the 30 routes answer 503 with a
truthful reason instead of serving.

### Decision: `backend/custom_agent_comparison.py`

I created it. It is the smallest thing that keeps `/api/comparison/run` alive
without inventing comparison metrics.

- **The custom (non-ADK) lane really runs.** It drives
  `StandaloneMemoryBankClient` over plain HTTPS, so four things are genuinely
  observed: wall-clock latency (`time.perf_counter`), the exact HTTP calls that
  crossed the wire (`MemoryBankClient.calls`), how many memories came back, and
  whether any SDK/ADK code was involved.
- **The ADK lane does not run.** Executing an `LlmAgent` needs the ADK runtime,
  a model endpoint and a live session, none of which this route owns. So that
  side reports its *declared configuration* — the roster from
  `list_all_agents()` with each agent's tools — plus `executed: false` and a
  stated reason. Every runtime figure on it renders `"n/r"`.
- **Tokens and answer quality are `"n/r"` on both lanes.** No tokenizer and no
  grader run inside this route.

The response carries a `reading_note` saying only one lane executed, so nobody
reads the n/r column as a bad score. Alternative considered and rejected:
deleting the route (endpoint list is frozen) or returning a canned comparison
table (the exact failure mode this rework exists to remove).

---

## 2. Problem B — frontend-to-backend wiring audit

Method: enumerated every `@app.route` in `backend/app.py` and every `fetch` /
`XMLHttpRequest` / `EventSource` in `frontend/static/js/app.js` with grep, then
matched them by path, method and response key.

**Confirmed the suspicion.** The original `app.js` contained exactly **6**
`fetch` calls, **0** `EventSource`, **0** `XMLHttpRequest`, against **30**
served routes. But the count understated the problem: of those 6 calls, 5 read
response keys the backend has never emitted, and the `/api/chat` call never
fired at all.

Line numbers: "UI caller" is the **new** `app.js`; the old line is given where
it differs. Backend lines are the **new** `app.py`.

| # | Endpoint | Method | UI caller (function · line) | Status |
|---|---|---|---|---|
| 1 | `/` | GET | browser navigation | WIRED |
| 2 | `/api/agents/list` | GET | none | UNCALLED |
| 3 | `/api/session` | GET | none | UNCALLED |
| 4 | `/api/testbed` | GET | `loadLiveTestbed` · 732 (was 666) | **FIXED** |
| 5 | `/api/eval-scorecard` | GET | `loadEvalScorecard` · 790 (was 696) | **FIXED** |
| 6 | `/api/chat` | POST | `submitAskQuestion` · 903 (was 746) | **FIXED** |
| 7 | `/api/session/channel/record` | POST | none | UNCALLED (alias of #6) |
| 8 | `/api/card-oversight` | GET | `loadCardOversight` · 1159 (was 854) | **FIXED** |
| 9 | `/api/card-oversight` | POST | none | UNCALLED (UI uses `/decide`) |
| 10 | `/api/card-oversight/decide` | POST | `submitCardDecision` · 1226 (was 891) | **FIXED** |
| 11 | `/api/compaction/run` | POST | `runMemoryCompaction` · 1265 (was 919) | **FIXED** |
| 12 | `/api/chat/stream` | GET | `subscribeToSynthesisStream` · 1080 | **FIXED** (was BROKEN: served SSE, nothing subscribed) |
| 13 | `/api/chat/stream` | POST | none | UNCALLED (GET variant is used) |
| 14 | `/api/session/channel/open` | POST | none | UNCALLED |
| 15 | `/api/session/channel/list` | GET | none | UNCALLED |
| 16 | `/api/claims/validate-and-write` | POST | none | UNCALLED |
| 17 | `/api/account/statement/<customer_id>` | GET | none | UNCALLED |
| 18 | `/api/knowledge-catalog/overview` | GET | none | UNCALLED |
| 19 | `/api/memory/get` | GET | none | UNCALLED |
| 20 | `/api/memory/retrieve` | POST | none | UNCALLED |
| 21 | `/api/memory/seed` | POST | none | UNCALLED |
| 22 | `/api/memory/ingest` | POST | none | UNCALLED |
| 23 | `/api/memory/compact` | POST | none | UNCALLED (UI uses `/api/compaction/run`, #11) |
| 24 | `/api/standalone/external-agent` | POST | none | UNCALLED |
| 25 | `/api/comparison/run` | POST | none | UNCALLED |
| 26 | `/api/audit/sweep` | GET, POST | none | UNCALLED |
| 27 | `/api/audit/customer/<customer_id>` | GET | none | UNCALLED |
| 28 | `/api/telemetry/<customer_id>` | GET | none | UNCALLED |
| 29 | `/api/admin/card-review` | GET | none | UNCALLED |
| 30 | `/api/admin/chat-verify` | POST | none | UNCALLED |
| 31 | `/api/admin/card-decision` | POST | none | UNCALLED |
| 32 | `/api/card/unlock` | POST | none | UNCALLED |

(32 rows for 30 `@app.route` decorators: `/api/card-oversight` and
`/api/chat/stream` each serve two methods and are listed per method.)

**No route was removed, renamed or merged.** Route set and method set were
diffed against the original — byte-identical.

**21 routes remain UNCALLED.** Per the brief that is acceptable and reported,
not fixed: inventing UI to call them would be a redesign.

### The breaks I fixed, in detail

Every one is a caller-side fix. The endpoint list is frozen and no route
signature changed.

**B1 — `/api/chat` never actually fired. (the worst one)**
`submitAskQuestion` called `openTrace("1", "policy")` at old line 730.
`openTrace` **is not defined anywhere** — not in `app.js`, not in `index.html`,
nowhere in the repo. It threw a `ReferenceError` before the `fetch`, so the Ask
console hung on every single question and the desk looked dead. Replaced with
`setTrace(true)`, which is the real function and does the intended thing.

**B2 — `/api/chat` response keys, five mismatches.**

| UI read | Backend emits | Consequence |
|---|---|---|
| `data.response` / `data.reply` | neither existed | fell through to the literal `"No response returned."` |
| `data.tool_sequence` | `tool_trajectory` | fell through to a **hardcoded four-tool list rendered on every answer** whether or not the desk had executed or said anything about tools |
| `data.veracity_audit` / `data.claim_veracity_audit` | `veracity_evaluation` | audit panel never rendered |
| `av.avenue` / `av.name` | `avenue_title` | avenue titles blank |
| `av.finding` / `av.evidence` / `av.result` | `finding_summary` | findings blank |
| `av.status` | `corroborates_claim` | every avenue labelled the default `"CHECKED"` |

All six now read the real keys, and each unwrapped through the provenance
helper. The hardcoded tool fallback is gone — no trajectory renders as a single
`n/r` tag.

**B3 — `/api/chat` sent an invalid channel.** The UI sent
`channel: "WEB_CHAT"`. `BankChannel` has no such member
(`FRAUD_DETECTION`, `TELEPHONY_IVR`, `MOBILE_APP`, `CORE_BANKING`,
`WEB_PORTAL`, `BRANCH_SUPPORT`), so the backend's `except ValueError` silently
relabelled **every web turn as `MOBILE_APP`**. Changed to `WEB_PORTAL`.

**B4 — `/api/chat/stream` was served and never subscribed to.** The four A2UI
frames — `onAgentThought`, `onMemoryBankAccess`, `onAgentSynthesis`,
`onUiComponentDelivery` — reached nobody, so the zero-question causal synthesis
that is the centre of the narrative arc never rendered. Added
`subscribeToSynthesisStream()`, an `EventSource` on `/api/chat/stream`, fired
right after the `/api/chat` POST returns. It renders into DOM ids that already
exist in `index.html` (`eventStreamList`, `askAnswerBody`, `caseCard1*`). Frame
names unchanged. No new UI was invented — those elements were already on the
page with nothing feeding them.

**B5 — `/api/testbed` response key.** UI read
`data.vertex_memory_bank.memories` then `data.memories`. Backend emits
`vertex_memory_bank_live_memories`. Neither read ever matched, so the live
memories panel was permanently empty and failed silently.

**B6 — `/api/card-oversight/decide` dropped the supervisor's reason.** UI sent
`supervisor_notes`; the route reads `admin_notes` or `reason`. The note never
reached the case file. Changed the caller to `admin_notes`. (I did not add
`supervisor_notes` to the backend — fixing the caller is the stated preference.)

**B7 — `/api/card-oversight` GET response key.** UI read `data.summary`;
backend emits `admin_review_case`. Now reads the real `AdminCardReviewCase`
fields (`flagged_transaction`, `flagged_amount`, `confidence_score`,
`verification_passed`, `recommended_admin_decision`).

**B8 — silent failure everywhere.** Five of six handlers did
`if (!resp.ok) return;` then `console.warn`. A 500 or 503 rendered as a page
that looked fine. Every handler now shows the upstream status and error text in
the panel it owns.

---

## 3. Problem C — scorecard provenance, end to end

### Backend

`GET /api/eval-scorecard` reads `results_memory_on.json` and
`results_memory_off.json` from `EVAL_RESULTS_DIR` (env var, defaults to
`<repo>/eval`). Every metric goes through one helper, `_metric()`, which emits:

```json
{ "id": "recall_at_k", "name": "Recall@5",
  "score":      {"value": 0.91, "_provenance": {"tag": "SEEDED",   "source": "results_memory_on.json#memory_layer.summary.recall_at_k"}},
  "display":    {"value": "91%","_provenance": {"tag": "COMPUTED", "source": "results_memory_on.json#memory_layer.summary.recall_at_k"}},
  "width_pct":  {"value": 91,   "_provenance": {"tag": "COMPUTED", "source": "..."}},
  "passed":     {"value": true, "_provenance": {"tag": "SEEDED",   "source": "..."}} }
```

If the harness did not record the key — or the file is absent — all four fields
become `unmeasured(source)`, whose value is the literal `"n/r"` and whose source
still names the exact file and JSON path that was missing. The response also
carries a `sources` block with the resolved absolute path and the read error, if
any, for each file.

**Every `.get(..., 1.0)` and every hardcoded `display` string is gone.** The old
version defaulted *thirteen* metrics to a perfect score and hardcoded strings
like `"0 leaks"`, `"10/10"`, `"1/1"`, `"2/2"`, `"pass (4/4)"`, `"12/12"` and
`width_pct: 100` — so a run with no eval files at all rendered as a flawless
scorecard. The whole `ab_comparison` block (`41200` / `12850` tokens, `68.8%`
saved, `6`/`0` questions re-asked, `2` contradictions, `14` withheld) was seven
literals with no producer anywhere in the repo; all seven now read from the
harness output or render `"n/r"`.

### Frontend

`app.js` gained a six-function provenance layer (`pvValue`, `pvTag`, `pvSource`,
`pvText`, `pvChip`, `renderMetric`). Rules it enforces:

- Reads `value` and `_provenance.tag`, renders **both** — the tag is a coloured
  chip next to the number, with the `source` string in the tooltip.
- `null`, `undefined` and `""` all render as the literal `"n/r"`. Nothing
  blanks and nothing zero-fills.
- Nothing is computed client-side to fill a gap. The old
  `data.memory_layer.cross_scope_leakage ?? 0` and
  `data.memory_layer.contradiction_handling || "10/10"` are both gone — and both
  read a key (`memory_layer`) the backend never emitted, so they *always* hit
  the fallback.
- The Recall@5 progress bar renders at 25% opacity with no width when the metric
  is unmeasured. A 0%-wide solid bar reads as "scored zero", which is a
  different claim from "not measured".

### The fabricated `finding_summary` block

`app.py` old lines ~515-525 asserted, as a corroborated finding:
`$1,000.00 charge used FALLBACK_MAGSTRIPE_KEYED entry (10.5x above $95.00
baseline)`. Nothing in the repo produces any of those four values. The
surrounding function contained roughly 250 lines of the same thing across four
presets — invented IP addresses (`198.51.100.4`, `203.0.113.19`), invented
timestamps (`09:05:12Z`, `09:17:12Z`), invented amounts (`£185.00`, `€620.00`,
`€1,480.00`), invented latencies (`0.31s`, `0.28s`, …) and invented device ids —
all presented as forensic findings.

The whole block is deleted. `_evaluate_interactive_workbench_turn` is now an
evidence gatherer:

- Scenario selection (`lisbon` / `contradiction` / `london` / `chicago`) is kept
  — it is an operator input, tagged `identity()`, never evidence.
- Each scenario maps to a declarative list of **probes** in `AVENUE_PLANS`. A
  probe is a real golden query, a real Memory Bank similarity search, or a real
  Dataplex glossary lookup.
- Each avenue returns the rows the probe actually returned, a `row_count`, the
  catalog verdict, any voluntary refusals logged, and a `finding_summary`
  computed strictly from those — `"golden_04_geo_impossible_travel returned 2
  row(s) under catalog verdict True"`. Never prose about what the rows mean.
- A probe that fails returns `"n/r"` plus the exact error.
- The verdict (`veracity_status`, `confidence_score`) is the
  `ClaimVeracityValidatorAgent`'s. If it is unavailable both read `"n/r"` with
  the reason. **There is no default verdict** — the old UI defaulted to
  `VERIFIED_TRUE` at `98%`, so a dead validator rendered as a confident pass.
- The four-step containment trajectory survives as the mandated tool *order*
  (tagged `identity()`, sourced to `CONTAINMENT_SEQUENCE`), because it is the
  policy the behaviour layer grades against. Per-step `status` and `latency` are
  `"n/r"` — nothing in this route executed them.

The narrative arc is intact: multi-channel friction → "why is nothing working" →
zero-question causal synthesis (now actually streaming, see B4) → admin YES/NO
card unlock.

---

## 4. Compaction / "Dreaming Service"

Both routes kept: `/api/memory/compact` and `/api/compaction/run` (the UI calls
the second). They share one implementation, `_compaction_report()`.

What it no longer does: any local compaction. No fragment merging, no
summarisation, no `CompactionReport` with `tokens_saved_estimate`.

What it does now:

1. `client.retrieve(scope)` → `memories_in_scope_before` — **LIVE_API**, real.
2. If the caller supplies `session_name`, `client.generate_from_session(...)` —
   this is where consolidation actually happens, **inside the managed Memory
   Bank**. Returns the operation name, **LIVE_API**.
3. If no `session_name`, `generate_operation` is `"n/r"` with the reason.
4. `consolidation_location` states, as an `identity()` value, that the work runs
   server-side.

Reported as `"n/r"`, permanently, with a stated reason: `tokens_before`,
`tokens_after`, `compression_ratio`, `token_reduction_pct`,
`nodes_consolidated`. The Memory Bank API does not return token counts, and the
generate operation is asynchronous and returns no consolidation count.

The UI button used to read `data.token_reduction_pct || "68.8%"` — and on the
`catch` branch it reset its own label to `"▶ Run compaction (68.8% saved)"`, so
**a failed request still displayed a 68.8% saving**. It now shows the real
memory count and the literal `n/r` for the saving.

---

## 5. Parameterisation

No project, engine, dataset or customer id is hardcoded at a call site. All are
module-level parameters with env-var defaults, matching the pattern
`backend/memory_bank.py` already uses in `create_default_memory_bank_client()`:

`GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `VERTEX_AGENT_ENGINE_ID`
(falling back to `AGENT_ENGINE_ID`, `VERTEX_MEMORY_BANK_ENGINE_ID`),
`BIGQUERY_DATASET`, `DATAPLEX_GLOSSARY`, `DEMO_CUSTOMER_ID`,
`DEMO_DEBIT_CARD_ID`, `DEMO_CREDIT_CARD_ID`, `DEMO_DEBIT_DISPUTE_ID`,
`DEMO_CREDIT_DISPUTE_ID`, `EVAL_RESULTS_DIR`, `GEMINI_MODEL`, `PORT`.

### One judgement call for you to overrule if you disagree

Deployment parameters (project id, engine id, dataset, glossary) are echoed in
responses tagged `identity(value, "env:NAME", "deployment parameter")`. They are
not measurements, so `live`/`seeded`/`computed` would all be lies; `static()` is
defined as a build failure. `identity()` is documented as waived for "an agent's
own name/channel", so this stretches it — but `identity()` is counted and
printed separately by the verifier, which is exactly the auditability these
values need. **If you want them out of the waiver list, say so and I will move
them to an untagged `config` block.**

---

## 6. Things wrong outside my three files

Ordered by how much they hurt.

1. **`backend/veracity_and_audit.py:37-38`** — imports `CustomerMemoryBank`,
   `GroundTruthTelemetryStore`, `KnowledgeCatalog`, none of which exist. This
   breaks `veracity_and_audit` *and* `adk_agents` (which imports it at line 49),
   and through them 13 routes. **This is the last blocker on the app starting
   cleanly.** Same fix as §1.

2. **`backend/models.py` field names do not match what `agent.py` was
   constructing.** The old `agent.py` built
   `CausalStep(step_number=, system=, day_time=, event=, impact=, status=)` and
   `SynthesisResult(customer_id=, narrative=, causal_steps=, asked_question=,
   resolution_action=, a2ui_payload=, confidence_score=)`. The real models are:
   - `CausalStep`: `step_number, timestamp, channel, event_title,
     causal_explanation, linked_fragment_id`
   - `SynthesisResult`: `customer_id, synthesized_at, executive_summary,
     root_cause_identified, causal_chain, recommended_actions,
     applicable_policies, confidence_score, zero_question_greeting`

   **Not one field name overlapped except `customer_id` and
   `confidence_score`.** Every SSE synthesis would have raised a pydantic
   `ValidationError`. I rewrote `agent.py` against the real models. Nobody could
   have hit this before because the module failed to import first.

3. **`models.py` — two fields should be optional.** `CausalStep.channel` is a
   required `BankChannel` with no unknown member, but Memory Bank retrieval does
   not return an originating channel. I currently set it to the *requesting*
   channel and say so in `causal_explanation`; `Optional[BankChannel]` would let
   it be honest. Likewise `SynthesisResult.confidence_score` is a required
   `float`, so it cannot hold `"n/r"` — I set it to `0.0` and put the honest
   `unmeasured()` value in the A2UI payload. `Optional[float]` would fix it.
   `models.py` is read-only shared code so I changed neither.

4. **`app.py` called `preload_context_for_external_agent(user_query=...,
   framework_name=...)`.** That method takes `(user_query, top_k)`;
   `framework_name` is a constructor argument. It would have been a `TypeError`
   on every `/api/standalone/external-agent` request. Fixed in my file.

5. **`tests/test_backend.py` will not pass as written.** It asserts on the old
   response shapes — `data["memory_layer"]`, hardcoded agent counts, fragment
   lists. The routes now return provenance-wrapped values and real service
   output. The test file needs updating by whoever owns it; I could not run it
   here in any case.

6. **`agents.json` and `README.md`** describe an 8-agent roster;
   `models.py:AgentId` and `adk_agents.list_all_agents()` define **6**;
   `app.js:AGENTS` renders **6**. I changed no count in either direction and
   pass `list_all_agents()` straight through. Flagging the inconsistency for
   whoever owns the architecture doc.

7. **`frontend/templates/index.html` ships hardcoded scorecard values** in the
   markup (`0`, `10/10`, `1/1`, `2/2`, `pass`, `12/12`). My JS overwrites the
   three ids it can reach (`scoreLeakage`, `scoreContradictions`, `scoreRecall`)
   with real values plus provenance, but the **Knowledge and Behaviour rows have
   no ids**, so they stay as static markup no script can correct. Whoever owns
   `index.html` should add ids to those six rows.

8. **`app.js:AGENTS` and `PIPELINE_STEPS` are simulated trace data** — invented
   span timings, refusal counts and tool counts for the black-bar trace. I left
   them exactly as they are: they are the manager's trace design, `index.html`
   labels the run `"simulation"`, and touching them would be a redesign.
   Flagging it so nobody mistakes that bar for measured telemetry.

---

## 7. What I verified, and what I could not

**Verified, by running it:**

- `backend/app.py` parses — `ast.parse`, Python 3.9.
- `backend/agent.py` parses — `ast.parse`.
- `backend/custom_agent_comparison.py` parses — `ast.parse`.
- `frontend/static/js/app.js` **genuinely parses in a real JavaScript engine.**
  macOS ships JavaScriptCore at
  `/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc`;
  I compiled the file with `new Function(source)`, which parses without
  executing, so `document`/`window` references cannot mask a syntax error. Both
  the original (as a control) and my version compile clean. This is stronger
  than the brace-balance check the brief asked for.
- **Route set and method set are byte-identical to the original** — extracted
  every `@app.route(...)` decorator from both files, sorted, `diff`. 30 in, 30
  out, zero difference.
- Every `fetch` / `EventSource` path in the new `app.js` matches a served route
  and method — checked by grep against the extracted route list.
- No `openTrace` reference survives outside a comment; all eleven new JS helper
  functions are defined in the file that calls them.
- No `_old` import remains in any file I own.

**Not verified — no way to, in this environment (no flask, pydantic, pytest,
node, network):**

- The app importing or starting. *Blocked in any case by item 1 in §6.*
- Any route returning anything. Every backend behaviour above is **written but
  unrun.**
- That the golden-query tool names and kwargs I call match at runtime. I read
  the signatures out of `adk_agents.py` and matched them by hand — `golden_01`,
  `02`, `03`, `04`, `07`, `09`, `10`, `11`. Note `golden_05`, `06` and `12` have
  contracts in `GOLDEN_QUERY_CONTRACTS` but **no callable wrapper function**, so
  I do not call them.
- That `MemoryBankClient` / `CatalogGovernor` return the response shapes I read.
  I took every field path from the client source and routed every read through
  `field()` / `mb_field()`, which returns `"n/r"` for anything absent — so a
  shape mismatch degrades to `n/r` rather than a `KeyError`.
- Any actual GCP call. Nothing here has ever reached a Google API.
- `tests/test_backend.py` — cannot run, and see §6 item 5.
- Rendering. No browser, so I have not seen a single pixel of the provenance
  chips.
