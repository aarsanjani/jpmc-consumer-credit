# REWORK REPORT — JPMC consumer-credit demo

## Where the demo is now

The demo used to look healthy and was not. It could not start at all: two
compatibility shims imported modules that were never in the repo, and a bare
`except Exception: pass` swallowed the error, so the failure surfaced three
files away as a confusing name error. Behind that, most of the internals were
theatre — a keyword matcher returning fabricated IP addresses and confidence
scores, a scorecard that defaulted thirteen metrics to a perfect pass when no
eval files existed, a compaction button that displayed "68.8% saved" even when
the request failed, and a chat console that threw a ReferenceError before it
ever called the backend. That is all removed. The code now calls the real
managed services — Vertex AI Memory Bank, Dataplex, BigQuery golden queries —
and where it has no real producer, it prints the literal `n/r` instead of a
plausible number. The architecture the manager signed off is untouched: eight
agents, hub-and-spoke, same thirty routes, same narrative arc. What none of this
proves is that it runs. Nobody had GCP credentials, so nothing in this set has
ever reached a Google API. It parses, the offline harnesses pass, and the first
live run is still ahead of you.

---

## Per plan item

### 1. Provenance harness

File: `backend/provenance.py` (new), `scripts/verify_provenance.py` (new).

Every value in a response now carries a tag saying where it came from: `seeded`
(a row from BigQuery or the seeded corpus), `live` (a call this process made),
`computed` (derived in process), `unmeasured` (not measured — renders as the
literal `n/r`), `identity` (a constant that is genuinely an identity, such as an
agent's own channel), and `static`, which is defined as a build failure and is
used nowhere. `verify_provenance.py` checks a payload for untagged or unwaived
static values.

Fixes: you can no longer tell a real number from an invented one by looking at
it. Now you can.

Caveat: this is the one item with no engineer's report. Its own verification
status is unknown to me. The other three workstreams import it and report clean
tag counts, which is indirect evidence it works, not a check of it.

### 2. Memory shape

Files: `backend/memory_bank.py`, `scripts/seed_atomic_memories.py` (new),
`scripts/memory_facts.py` (new).

A local "Dreaming Service" had written one ~200-word narrative into Memory Bank
as a single memory. That breaks retrieval twice. Semantic search over a set of
one is meaningless — "where is the customer", "what did they ask us not to do"
and "which cards were lost" all return the same paragraph. And consolidation has
nothing to supersede: Memory Bank replaces a fact with a newer fact, and it
cannot replace one clause inside a paragraph, so "travelling until the twelfth"
outlives the trip forever.

The blob is replaced by nine atomic facts for CUST00001, each a restatement of a
single turn in the seeded 09 Sep IVR call. `memory_bank.py` gained
`create_memory` and `delete_memory` — 37 lines reusing the client's existing
request path — because the client could write a conversation but not a single
known fact, and could not remove one.

`memory_facts.py` is a write-time guard, not a convention. It refuses any memory
carrying a currency amount, a bare quantity, a dispute id, a dated deadline, a
duration window or a record id, and the refusal names the matched text, the
rule, the character offsets and why that class of value belongs in BigQuery.

Fixes: retrieval has something to rank, consolidation has units to supersede,
and no memory can become a second source of truth for a number that BigQuery
owns.

### 3. Scenario alignment against BigQuery

File: `scripts/reconcile_scenario.py` (new).

The stored memory asserted EUR 412, EUR 1480 and a 90-day window. The
authoritative disputes are in USD. Two of the three were wrong in the currency
as well as the amount.

The third is the interesting one. "90" happens to be right, and the reconciler
still fails on it, reporting `MATCHES-BUT-STILL-WRONG-HOME`. The window is
policy plus the filing date, both looked up; a memory holding it is right only
until either changes, and nothing marks it stale when they do. The standard is
not "the number agreed", it is "the number is not in memory".

Exit codes are the point: 0 for pass, 1 for a real contradiction, 3 for
"could not check". An unread BigQuery is `n/r` and a non-zero exit, never a
green tick.

### 4. Five channel agents on real seeded sessions

File: `backend/adk_agents.py`.

`list_all_agents()` was returning six agents with a docstring claiming six,
against a README and a test that both say eight. The five channel producer tools
named in the README — fraud velocity, IVR call records, mobile wallet events,
web portal activity, branch teller interactions — did not exist anywhere in the
repo. Not as hardcoded dicts; they had already been deleted. They were written
fresh.

All five wrap one function that lists real Memory Bank sessions, keeps the ones
matching both the customer scope and the agent's channel, reads the real events
for each, and runs a scoped `retrieve` for consolidated memories. Similarity
search and ranking happen inside Google. There is no vector maths, no cosine
similarity and no client-side re-ordering in the file.

The roster is back to eight with the real hub-and-spoke topology — synthesizer
as hub, seven spokes, each built through one helper that sets the no-transfer
guardrails so they cannot be forgotten on a new spoke. Nothing was deleted: all
22 original functions are still present.

Also fixed here: `execute_golden_query_contract` used to catch every BigQuery
exception and return `rows = [{"note": "BigQuery execution note: ..."}]`, which
made `row_count = 1` and put an error string where a caller expects a row. A
missing table, a bad credential or an absent SQL file all came back as a
success-shaped payload. It now raises.

Behaviour change worth knowing: endpoints that call golden tools will surface a
500 when BigQuery is unreachable, instead of quietly returning a one-row
"result". That is the intended loud failure. How it should render in the UI is
an open decision.

### 5. Claim validator on BigQuery golden queries

File: `backend/veracity_and_audit.py`.

Deleted: about 210 lines of `if "lisbon" in claim_lower` keyword matching that
returned hand-written verdicts with hand-written IP addresses, timestamps and a
`confidence_score` of 0.96. Also deleted: a ~440-line prompt block of scenario
instructions ("If the claim is about London Heathrow Duty Free... DO NOT mention
the Chicago $1,000 charge") that steered the model to a predetermined story. The
new prompt contains no scenario, no city, no amount and no merchant — only rules
about how to treat the evidence it is given.

The verdict now comes from one Gemini structured-output call over rows that
golden queries 01, 02, 07 and 10 actually returned. Every query is
parameterised; the card id is derived from the customer-360 result, never a
literal. A `logging.warning("...fallback triggered")` path that produced a
fabricated verdict when Vertex was unreachable is replaced with a raise.

Two avenues that used to be fabricated now honestly report nothing: travel
notices and contact history have no golden query contract covering them, so they
record `unmeasured` with the reason. They will start returning real rows the day
someone registers those contracts, with no edit to the file. A third — the
"Multi-Step SMS Y/N Consent Audit / eSIM clone" avenue — was deleted outright.
There is no SMS-consent table in the 12-table dataset. It was fiction built on a
dict, and nothing replaces it.

The validator also detects the swallowed-BigQuery-error row shape described in
item 4 and refuses it, in case an older `adk_agents.py` is in place.

### 6. Catalog authorization traps

File: `backend/veracity_and_audit.py`, using `CatalogGovernor` from
`backend/knowledge_catalog.py`.

Before reading a table, the validator asks Dataplex. The deprecated-table trap
fires on `authorizations_deprecated_v1`: the catalog returns `REFUSE` citing
`lifecycle-status.status = DEPRECATED` and names `authorizations` as the
replacement, and the agent declines.

The design detail that makes this real: the code does not know which of the two
candidates is deprecated. Both go to the catalog in the same loop with the same
call. No string in the file matches "deprecated" and nothing branches on the
table name. Flip the aspect in Dataplex and the agent declines the other table.

The restricted-column trap fires on `customers.ssn_last4` and `cards.pan_full`,
acting on the `sensitivity-class.agent_behavior` aspect rather than the
human-facing label. Declining is not just a log line: every result set is
redacted for declined column names before it is emitted, so the evidence record
cannot claim a refusal that the output contradicts. A control column,
`pan_masked`, is probed too and is expected to come back ALLOW — that proves the
gate reads answers rather than refusing everything.

Every record states plainly that Dataplex advises and does not enforce. The
agent's decision to decline is the control being demonstrated.

### 7. Removing the `_old` compatibility shim

Files: `backend/memory_bank.py`, `backend/knowledge_catalog.py`, and the call
sites in `backend/app.py`, `backend/agent.py`, `backend/veracity_and_audit.py`.

This is the item with the widest blast radius, and it is covered in Findings
below.

Both files ended with a "Backward Compatibility Bridge" that imported four names
from `backend.memory_bank_old` and `backend.knowledge_catalog_old` inside
`try: ... except Exception: pass`. Neither module exists anywhere in the repo.
Both bridges are gone, along with the stale section headings that named them. No
replacement shim was added.

### 8. Frontend-to-backend wiring

Files: `frontend/static/js/app.js`, `backend/app.py`.

The original `app.js` had six `fetch` calls against thirty served routes, no
`EventSource` and no `XMLHttpRequest`. Of those six, five read response keys the
backend has never emitted, and the sixth never fired.

| What was broken | Effect |
|---|---|
| `submitAskQuestion` called `openTrace()`, which is not defined anywhere | ReferenceError before the fetch. `/api/chat` never fired. The Ask console hung on every question. |
| UI read `data.response`, backend emits neither that nor `data.reply` | Always rendered "No response returned." |
| UI read `data.tool_sequence`, backend emits `tool_trajectory` | Fell through to a hardcoded four-tool list shown on every answer. |
| UI read `data.veracity_audit`, backend emits `veracity_evaluation` | Audit panel never rendered. |
| UI sent `channel: "WEB_CHAT"`, which is not a `BankChannel` member | Backend's `except ValueError` relabelled every web turn as `MOBILE_APP`. |
| `/api/chat/stream` was served and nothing subscribed | The zero-question causal synthesis — the centre of the narrative — never rendered. |
| `/api/testbed` read `data.memories`, backend emits `vertex_memory_bank_live_memories` | Live memories panel permanently empty, silently. |
| `/api/card-oversight/decide` sent `supervisor_notes`, route reads `admin_notes` | The supervisor's reason never reached the case file. |
| Five of six handlers did `if (!resp.ok) return;` then `console.warn` | A 500 or 503 rendered as a page that looked fine. |

All fixed on the caller side. The endpoint list is frozen: the route set and
method set were diffed against the original and are byte-identical. Twenty-one
routes remain uncalled by the UI — that is reported, not fixed, since inventing
UI to call them would be a redesign.

`subscribeToSynthesisStream()` renders into DOM ids that were already in
`index.html` with nothing feeding them. No new UI was invented.

### 9. Scorecard provenance

Files: `backend/app.py`, `frontend/static/js/app.js`.

The old scorecard defaulted thirteen metrics to a perfect score with
`.get(..., 1.0)` and hardcoded display strings — "0 leaks", "10/10", "1/1",
"2/2", "pass (4/4)", "12/12", `width_pct: 100`. A run with no eval files at all
rendered flawless. The whole A/B comparison block (41200 and 12850 tokens, 68.8%
saved, 6 versus 0 questions re-asked, 2 contradictions, 14 withheld) was seven
literals with no producer anywhere in the repo.

Now every metric goes through one helper that emits a value, a provenance tag
and the exact file and JSON path it came from. A missing key or a missing file
produces `n/r` with the path that was looked for. The frontend renders the tag
as a chip with the source in the tooltip; `null`, `undefined` and empty string
all render `n/r`. The Recall@5 bar renders at 25% opacity with no width when
unmeasured, because a 0%-wide solid bar reads as "scored zero", which is a
different claim from "not measured".

The compaction button is a good example of the old pattern: it read
`data.token_reduction_pct || "68.8%"`, and on the error branch reset its own
label to "Run compaction (68.8% saved)". A failed request displayed a 68.8%
saving. Token counts are now permanently `n/r` with a stated reason — the Memory
Bank API does not return them, and the generate operation is asynchronous and
returns no consolidation count. Consolidation itself now runs where it belongs,
server-side in Memory Bank, via `generate_from_session`.

### 10. README accuracy

Not delivered. `README.md` is not in the sixteen-file set and no engineer owned
it. Details under "Out of scope / still open".

---

## Verified vs unrun

This is the section to read twice if you read nothing else.

Nobody on this rework had GCP credentials. The build environment was Python
3.9.6 with no flask, no pydantic, no pytest, no google-cloud SDK, no dotenv, no
certifi and no network. Nothing in these sixteen files has ever reached a Google
API. No BigQuery row, no Dataplex aspect, no Memory Bank record and no Gemini
response was observed by any of the four engineers.

### Verified by running it

| Check | Scope | Result |
|---|---|---|
| `ast.parse` | all delivered `.py` files, `app.js` parsed separately | passes |
| Python 3.9 syntax only — no `match`, no `X \| Y` unions | delivered backend files | passes |
| `GroundTruthTelemetryStore`, `CustomerMemoryBank`, `KnowledgeCatalog`, `memory_bank_old`, `knowledge_catalog_old` | `veracity_and_audit.py` | 0 occurrences |
| `api_called`, `text-embedding`, `cosine`, `fallback triggered`, `static(` | `veracity_and_audit.py` | 0 occurrences |
| Import of `memory_bank` and `knowledge_catalog` | reproduced the original `ImportError`, then showed both importing clean after the bridge removal, with `create_memory` and `delete_memory` present | passes |
| AST diff of `adk_agents.py` against the original | 22 original functions still present, 14 added, none removed or renamed | confirmed |
| Roster extracted from the `list_all_agents()` AST | 8 descriptors, names and order matching `tests/test_backend.py` and the README table | confirmed |
| Guardrail flags on the spoke constructor; 7 spokes; hub tools and `sub_agents` | from the AST | confirmed |
| `except` handlers in the channel producer layer | AST walk over all ten functions | 0 |
| `static()` call sites; `identity()` call sites | `adk_agents.py`, by AST | 0 static, 1 identity |
| Route and method set of `app.py` | diffed against the original, 30 decorators / 32 route-method pairs | byte-identical |
| Stub-harness acceptance tests on the channel producers | 5 tests, described below | all pass |
| `scripts/memory_facts.py` | executed | exit 0, 12 rejects, 9 accepts, no false positives |
| `scripts/seed_atomic_memories.py` dry-run and refusal paths | executed | exit 0, prints the plan, refuses without ids |
| `scripts/reconcile_scenario.py` offline paths | four runs, described below | exit codes 0 / 3 / 1 / 0 as designed |

The stub harness is the strongest evidence in the set, so it is worth being
precise about what it is. It lifts the channel producer functions out of the
source with `ast`, then runs that actual code against the real `provenance.py`,
the real field-reading helper from `memory_bank.py`, and a fake REST client. It
confirmed five things by execution: seeded sessions flow through with correct
tags and a session belonging to another customer is excluded by the scope
filter; with the corpus emptied the producer returns empty lists and zero counts
rather than any row; a client that raises propagates the error out untouched; an
event missing its timestamp renders `n/r`; and the provenance summary comes back
clean with one waived constant.

The four reconciler runs: dry-run with no input gives exit 0 and `NO VERDICT`,
stating it read nothing and proved nothing. The pre-fix claims with no
authoritative row give exit 3 and `INCONCLUSIVE`. The pre-fix claims against a
local BigQuery fixture give exit 1 with two `MISMATCH` and one
`MATCHES-BUT-STILL-WRONG-HOME`. The nine post-fix facts against the same fixture
give exit 0 and `PASS`.

### Not run

| Not run | Why it matters |
|---|---|
| Any call to Vertex AI, Memory Bank, Dataplex, BigQuery or Gemini | Every behaviour described in this report above the parse line is written, not observed. |
| Importing any backend module with its real dependencies | flask, pydantic and the Google SDK were absent. Import-time side effects — the default Memory Bank client and catalog governor constructors — were never seen to succeed. |
| `python3 -c "import backend.app"` | Never attempted. This is the first meaningful check after you apply. |
| `LlmAgent(...)` construction | Never built. The set relies on `disallow_transfer_to_parent` and `disallow_transfer_to_peers` being accepted kwargs of the installed ADK version. The README says so; the package was not there to confirm. |
| Any pydantic model construction | `AgentDescriptor` and the rest were matched to their declared field types by hand. Nothing enforced it. |
| `tests/test_backend.py` | Never run. See "still open" — it will not pass as written. |
| `/api/agents/list` and every other route | The 8-agent assertion was checked against the AST, not against a live response. |
| Rendering | No browser. Not one pixel of the provenance chips has been seen. The SSE subscriber has never received a frame. |
| `create_memory`, `delete_memory`, the operation-wait loop, the live purge | Request shapes follow the documented v1beta1 REST surface and reuse the client's existing request path, but nobody has seen a 200 from them. |
| The `create` response shape | The new memory's resource name is read from `operation.response.name`. If the service nests it differently the script prints `n/r` rather than crashing. Worth a five-minute check on the first live run. |
| The real BigQuery column names | `backend/queries/golden/*.sql` is absent from the repo copy, so the disputes schema could not be read. `disputed_amount_usd`, `provisional_credit_due_date` and `dispute_id` were taken from `models.py`. They are a guess. The reconciler tries a candidate list, prints the column it used, and reports `UNVERIFIABLE` when none matches. |
| Whether the disputes really are USD 1,284.60 and USD 3,940.15 | Those figures came from the task statement. Nothing in the delivered set asserts them — they exist only in a local test fixture that was not shipped. The first live reconciler run is what confirms them. |
| Whether CUST00001's Lisbon trip starts on the 3rd | The transcript only says the travel notice runs "through the twelfth". The end date is grounded; the start date is not. Either read it from the travel-notice row, or trim fact 1 to "Travelling in Lisbon until 12 Sep". |
| The channel token vocabulary against the 52 seeded sessions | The largest runtime risk in the set. If the corpus spells a channel in a way the alias table does not cover, that producer returns empty. Check it first — the failure mode is under-reporting, visible as `session_count: 0`, never a fabricated row. |
| The new raise in `execute_golden_query_contract` | BigQuery was never reached, so the loud-failure path is unexercised. |
| `backend/provenance.py` and `scripts/verify_provenance.py` | No report covers them. I cannot tell you what was checked or what good output looks like. |
| Golden query tool names and kwargs matching at runtime | Read out of `adk_agents.py` and matched by hand. Note that golden 05, 06 and 12 have contracts but no callable wrapper, so nothing calls them. |

One thing to hold onto: "checked, found nothing" and "nothing to check" print
different words throughout this set, on purpose. A `n/r` avenue is not evidence
for a claim, and an unread source is not a clean bill of health.

---

## Findings you should know

### The `_old` shim meant `app.py` was running the old fake code

This is the finding that explains the symptom you were seeing. Writes worked and
retrieval returned local JSONL.

`app.py`, `agent.py` and `veracity_and_audit.py` all imported
`CustomerMemoryBank`, `GroundTruthTelemetryStore` and `KnowledgeCatalog` from
`backend.memory_bank` and `backend.knowledge_catalog`. Those names were not
defined in either file. They were re-exported at the bottom of each from
`backend.memory_bank_old` and `backend.knowledge_catalog_old`, inside
`try: ... except Exception: pass`. Neither `_old` module is in the repo.

So the import always failed, the bare `except` always swallowed it, and the
names resolved to nothing. The real REST clients — `MemoryBankClient`,
`CatalogGovernor` — were sitting right there in the same files, unused by the
app. Anything routed through the phantom names went to the legacy local path.
The bare except is the whole defect: it turned a missing module into silence, so
the failure surfaced three files away as a name error instead of at the line
that caused it.

Both bridges are removed and every call site now goes through the real client.

### `app.py` reported `"ONLINE"` as string literals

The health endpoint returned `"ONLINE"` as hardcoded strings rather than the
result of any probe. A service that was down reported as up. Note that no
engineer's report mentions the health endpoint, so I cannot tell you how the
replacement `app.py` treats it. Check it yourself after applying: hit the health
route with the backing services unreachable and confirm it does not say ONLINE.

### `agent.py` used field names that do not exist in `models.py`

The old `agent.py` constructed `CausalStep(step_number=, system=, day_time=,
event=, impact=, status=)` and `SynthesisResult(customer_id=, narrative=,
causal_steps=, asked_question=, resolution_action=, a2ui_payload=,
confidence_score=)`.

The real models are `CausalStep(step_number, timestamp, channel, event_title,
causal_explanation, linked_fragment_id)` and `SynthesisResult(customer_id,
synthesized_at, executive_summary, root_cause_identified, causal_chain,
recommended_actions, applicable_policies, confidence_score,
zero_question_greeting)`.

Not one field name overlapped except `customer_id` and `confidence_score`. Every
SSE synthesis would have raised a pydantic ValidationError. Nobody could have
hit this before, because the module failed to import first. `agent.py` is
rewritten against the real models.

### Other finds worth a minute

`preload_context_for_external_agent` was called with `framework_name=`, which is
a constructor argument, not a method parameter. Every
`/api/standalone/external-agent` request would have been a TypeError. Fixed.

`veracity_and_audit.py` was keyword-matching on the literal strings "1480" and
"620" to decide whether a claim was corroborated. That hard-wired the audit
sweep to the pre-fix numbers, so it would have failed to corroborate the correct
BigQuery amounts. Gone with the rest of the keyword logic.

`evaluate_customer_chat_verification` still accepts the `scenario` parameter
because `app.py` passes it, but it can no longer affect the outcome. It is
recorded as a hint that was not honoured. The branch in `app.py` that computes
it is now dead weight.

---

## Out of scope / still open

Ordered by how much they hurt.

> **Items 1–3 below were checked against the actual repo after this report was
> drafted and are NOT open.** They were written by engineers who did not have
> those files in view. Kept here, struck through, so the correction is visible
> rather than silently edited away.

1. ~~`backend/custom_agent_comparison.py` is missing.~~ **Not true.** The file
   exists in the repo (21 KB, `class AgentComparisonEngine` at line 303) and the
   reworked `backend/app.py` imports it at line 63 and calls it at line 1368.
   Nothing to do.

2. ~~`backend/queries/golden/` is absent.~~ **Not true.** The directory exists in
   the repo and the demo kit carries all twelve `.sql` files plus `INDEX.md`.
   The loader reads `backend/queries/golden/`, which is present. Nothing to do.

3. ~~`README.md` was not touched.~~ **Not true.** A rewritten `README.md` is in
   the delivered set. It removes the `GroundTruthTelemetryStore` description, the
   `_old` modules, the local-embedding and cosine-similarity claims, and the
   catalog-enforcement language, and it describes each channel producer by what
   it reads rather than by what it will find.

4. `backend/models.py` was treated as read-only shared code and nobody changed
   it. Three consequences. `AgentId` still enumerates the six-agent drift
   (`01_orchestrator` through `06_memory_writer`), so the golden query contracts
   authorize agents by a vocabulary the published roster no longer uses — the
   validator is agent 6 of 8 and is not in the enum at all, so it borrows the
   golden wrappers' declared identity rather than calling with its own.
   `AgentDescriptor` has no `agent_type` field, so the README's
   CHANNEL_PRODUCER / PRE_WRITE_VALIDATOR / CONSOLIDATED_AUDITOR /
   LEAD_SYNTHESIZER classification is folded into `role`. And several fields are
   required where they should be optional — `SynthesisResult.confidence_score`
   and `AdminCardReviewCase.confidence_score` are required floats, so they
   cannot hold `n/r`; they are set to `0.0` with the honest unmeasured value
   carried alongside in the payload.

5. `tests/test_backend.py` will not pass as written. It asserts on the old
   response shapes — `data["memory_layer"]`, fragment lists, hardcoded values.
   The routes now return provenance-wrapped values. The 8-agent roster assertion
   should now pass; the rest need updating by whoever owns the file. Nobody
   could run it.

6. `frontend/templates/index.html` ships hardcoded scorecard values in the
   markup: `0`, `10/10`, `1/1`, `2/2`, `pass`, `12/12`. The new JS overwrites
   the three ids it can reach, but the Knowledge and Behaviour rows have no ids,
   so they stay as static markup that no script can correct. They need ids, or
   the literals need removing from the template.

7. The scenario currency is inconsistent across the repo. `index.html` states
   EUR 620 and EUR 1,480.00 for DSP00033 and DSP00034 in UI copy, and hardcodes
   the derived Reg E date (23 Sep 2026) and the 90-day window rather than
   deriving them from the filing date. The authoritative BigQuery amounts are in
   USD. The three backend files that also held those literals were replaced, but
   the template was not, so the seam will be visible to a reader.

8. `agents.json` lists three agents and has no entry for the five channel
   producers. It should list eight to match the README and the API.

9. Two validator avenues report `n/r` by design because no golden query contract
   covers `travel_notices` or `contact_events`. Register contracts for those two
   tables and both avenues start returning real rows with no code change.

10. Golden queries 05, 06 and 12 have contracts in `GOLDEN_QUERY_CONTRACTS` but
    no callable wrapper function, so nothing calls them.

11. `backend/memory_bank.py` seeds `sessionState.channel = "IVR"` while
    `models.py` calls the same channel `TELEPHONY_IVR`. The channel alias table
    absorbs the mismatch today. The seeder should write the enum value so the
    alias table can shrink.

12. One judgement call flagged for you to overrule. Deployment parameters —
    project id, engine id, dataset, glossary — are echoed in responses tagged
    `identity(value, "env:NAME", "deployment parameter")`. They are not
    measurements, so `live` / `seeded` / `computed` would all be wrong, and
    `static()` is defined as a build failure. `identity()` is documented as
    waived for an agent's own name or channel, so this stretches it. The
    argument for it is that `identity()` is counted and printed separately by
    the verifier, which is the auditability these values need. If you want them
    out of the waiver list, they can move to an untagged config block.
