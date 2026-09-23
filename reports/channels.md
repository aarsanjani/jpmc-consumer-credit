# `backend/adk_agents.py` — roster restoration and channel producers

One file changed: `backend/adk_agents.py`. Nothing else was touched.

## 1. The drift I found, and what the roster is now

`list_all_agents()` was returning six agents — `01_orchestrator`, `02_identity_context`,
`03_fraud_triage`, `04_policy_compliance`, `05_resolution_actions`, `06_memory_writer` —
with a docstring that said "6 CCB Fraud & Disputes Desk agents". That contradicted the
README roster table (8 agents, hub-and-spoke) and `tests/test_backend.py`, which asserts
`data["count"] == 8` and the eight snake_case names.

The drift went further than the descriptor list. **The five channel producer tools named in
the README did not exist anywhere in the repo.** `query_fraud_velocity_alerts`,
`query_ivr_call_records`, `query_mobile_wallet_events`, `query_web_portal_activity` and
`query_branch_teller_interactions` appear only in README prose — `grep` across `backend/`,
`tests/` and `frontend/` finds no definition and no caller. So the brief's "the five channel
producers return hardcoded dicts" describes a state earlier than the code I was given: by the
time it reached me the producers had been deleted outright. I wrote them fresh, data-backed,
rather than porting hardcoded versions.

The roster `list_all_agents()` now returns, in this order:

| # | `name` (what the test asserts) | `agent_id` | `channel` | tools |
|---|---|---|---|---|
| 1 | `fraud_monitoring_agent` | `01_fraud_monitoring_agent` | `FRAUD_DETECTION` | `query_fraud_velocity_alerts`, `golden_03_…`, `golden_04_…` |
| 2 | `telephony_ivr_agent` | `02_telephony_ivr_agent` | `TELEPHONY_IVR` | `query_ivr_call_records`, `golden_02_…` |
| 3 | `mobile_app_agent` | `03_mobile_app_agent` | `MOBILE_APP` | `query_mobile_wallet_events`, `golden_10_…` |
| 4 | `web_portal_agent` | `04_web_portal_agent` | `WEB_PORTAL` | `query_web_portal_activity`, `golden_07_…` |
| 5 | `branch_support_agent` | `05_branch_support_agent` | `BRANCH_SUPPORT` | `query_branch_teller_interactions`, `golden_01_…` |
| 6 | `claim_veracity_validator_agent` | `06_claim_veracity_validator_agent` | — | `validate_and_record_customer_claim` |
| 7 | `memory_bank_audit_agent` | `07_memory_bank_audit_agent` | — | `run_consolidated_memory_audit_sweep` |
| 8 | `consumer_credit_synthesizer_agent` | `08_consumer_credit_synthesizer_agent` | `CORE_BANKING` | `read_customer_memory_bank`, `execute_one_click_card_unlock` |

Shape notes, so nobody has to guess why fields hold what they do:

- The existing `AgentDescriptor` model (`backend/models.py`) is used unchanged:
  `agent_id`, `name`, `role`, `channel`, `model`, `tools`, `description`. No new shape.
- `name` carries the ADK agent name because `tests/test_backend.py` reads `a["name"]` and
  compares against `fraud_monitoring_agent` etc. The old file put a display label
  ("01 Orchestrator") there, which is the specific thing that made the test unsatisfiable.
- `AgentDescriptor` has no `agent_type` field and I do not own `models.py`, so the README's
  type (`CHANNEL_PRODUCER`, `PRE_WRITE_VALIDATOR`, `CONSOLIDATED_AUDITOR`, `LEAD_SYNTHESIZER`)
  is carried in `role`, e.g. `"Fraud Velocity Specialist (CHANNEL_PRODUCER)"`. If a first-class
  field is wanted, that is a `models.py` change — see §7.
- Agents 6 and 7 are listed here but implemented in `veracity_and_audit.py`; this file only
  wires the existing thin wrappers `validate_and_record_customer_claim` and
  `run_consolidated_memory_audit_sweep` as their tools.

Topology: `create_consumer_credit_synthesizer_agent()` now builds the real hub-and-spoke —
the synthesizer as hub, the other seven as `sub_agents`. Every spoke is built through one
helper, `_spoke_agent()`, which sets `disallow_transfer_to_parent=True` and
`disallow_transfer_to_peers=True`, so the guardrail cannot be forgotten on a new spoke.
Previously this factory was a one-line alias onto the 6-agent orchestrator.

Nothing was deleted. All 22 original functions are still present and unrenamed — the golden
query contract registry, `load_golden_sql_text`, `execute_golden_query_contract`,
`golden_01` … `golden_12`, the catalog governance tools, the legacy wrappers, and the
6-agent `create_ccb_fraud_desk_orchestrator_agent` factory (kept, with a comment saying it is
the retained internal specialist tree and the published desk is the hub-and-spoke). The 8
agents sit on top of that substrate and reference the golden tools where the channel has a
ledger counterpart: IVR → authorizations (02), mobile wallet → active tokens (10), web portal
→ open disputes (07), fraud monitoring → suspicious window and impossible travel (03, 04),
branch → customer 360 (01).

## 2. How each producer gets its data

All five are three-line wrappers over one function, `_produce_channel_payload()`, which does:

1. `bank = _memory_bank(client)` — the engine-level `MemoryBankClient` singleton, never
   customer-bound at construction.
2. `scope = {"customer_id": customer_id}` — built per call, passed per call.
3. `bank.list_sessions()` → keep a session only if **both** hold:
   - its customer matches the scope: `sessionState.customer_id`, falling back to `userId`;
   - its `sessionState.channel` matches the agent's own channel.
4. For each kept session, `bank.list_events(session_name)` for the real turns.
5. `bank.retrieve(scope=scope, query=<channel query>, top_k=top_k)` for the consolidated
   memories. Similarity search and ranking run inside Google — there is no vector maths,
   no cosine similarity and no client-side re-ordering anywhere in this file.

Everything in the returned payload except the channel name comes from those responses.

**Channel matching.** The canonical channel is the `BankChannel` constant. The seeded corpus
does not always spell it the same way — `memory_bank.py`'s own Lisbon seeder writes
`sessionState.channel = "IVR"`, not `"TELEPHONY_IVR"` — so `CHANNEL_SESSION_TOKENS` maps each
canonical channel to the spellings that mean it (`TELEPHONY_IVR` ← `IVR`, `TELEPHONY`,
`PHONE`, `CONTACT_CENTER`). Those tokens are a matching predicate in code; the label the agent
reports back is `channel_label_in_data`, read from the session itself, so a reader can see
which spelling the data actually used. If the seeder's vocabulary is wider than this list,
extra sessions are *missed*, never invented — the failure mode is under-reporting, which is
visible as a low `session_count`, not a fabricated row.

**Parameters, no hardcoded ids.** `DEMO_CUSTOMER_ID` (default `CUST00001`),
`CHANNEL_MEMORY_TOP_K` (default 5) and `DEMO_CARD_ID` (default `CARD00002`) are env-var
defaults on the tool signatures. Project, engine and location stay where they already were,
on `MemoryBankClient` via `create_default_memory_bank_client()`; this file adds no new
project/engine/dataset literal.

## 3. Exact client calls

Read from `backend/memory_bank.py` and called as-is — nothing reimplemented:

| Call | Signature used | Where |
|---|---|---|
| `MemoryBankClient.list_sessions()` | `() -> List[Dict[str, Any]]` | `_produce_channel_payload` |
| `MemoryBankClient.list_events(session_name)` | `(session_name: str) -> List[Dict[str, Any]]` | `_event_rows` |
| `MemoryBankClient.retrieve(scope, query, top_k)` | `(scope: Dict[str,str], query: Optional[str]=None, top_k: int=5, memory_filter: Optional[str]=None)` | `_memory_rows` |
| `MemoryBankClient.create_session(user_id, display_name, session_state)` | `(user_id: str, display_name: Optional[str]=None, session_state: Optional[Dict]=None) -> str` | `execute_one_click_card_unlock` |
| `MemoryBankClient.append_event(session_name, author, text, …)` | `(session_name: str, author: str, text: str, timestamp=None, invocation_id=None) -> Dict` | `execute_one_click_card_unlock` |
| `memory_bank.field(obj, *path, default="n/r")` | already imported as `mb_field` | every field read |

`CustomerMemoryBank` is **not** used. In the repo it is only a backward-compat name imported
inside a `try:` from `backend.memory_bank_old`, a module that does not exist here, so it
resolves to `None` at import time. Using it would have been a null dereference. Everything
goes through `MemoryBankClient`, which is the class that actually makes the REST calls.

## 4. The delete-the-data acceptance test

Satisfied by construction: every value in the payload is derived from a loop over a service
response, so no data means no iterations.

- `sessions` and `memories` are built by appending inside `for` loops over `list_sessions()`,
  `list_events()` and `retrieve()`. Empty responses → empty lists.
- `session_count` / `memory_count` / `event_count` are `len()` of those lists → `0`, and they
  are `computed`, so the zero is a measured zero, not a default.
- There is **no `try`/`except` anywhere in the producer layer** (verified by AST walk over all
  ten functions: zero handlers). A `MemoryBankError` from the REST client propagates straight
  out to the caller.
- No literal row, no "sample", no "example", no fallback branch. Missing scalar fields render
  the literal `"n/r"` via `_tagged_value()`, which returns `unmeasured(source)` whenever the
  service omitted a value.

I also fixed a fabrication I inherited in this file: `execute_golden_query_contract()` caught
every BigQuery exception and returned `rows = [{"note": "BigQuery execution note: …"}]`, which
made `row_count = 1` and put an error string where a caller expects a row. It now raises
`RuntimeError` naming the query id and the underlying error. **Behaviour change worth
flagging:** endpoints in `app.py` that call golden tools will now surface a 500 when BigQuery
is unreachable instead of quietly returning a one-row "result". That is the intended loud
failure, but whoever owns `app.py` should decide how it is rendered to the UI.

## 5. Provenance tagging

| Tag | Used for |
|---|---|
| `identity(...)` | Exactly one call site in the whole file: the agent's own channel constant, sourced `"<agent>.channel"`, reason `"agent identity constant"`. Verified by AST — one `identity()` call, no others. |
| `seeded(v, "sessions.list:<session_name>")` | Every session field: id, resource name, display name, channel label, customer id, intent, create/update time. |
| `seeded(v, "sessions.events.list:<session_name>")` | Every event field: author, timestamp, text, invocation id. |
| `seeded(v, "memories.retrieve:<memory_id>")` | Every memory field: id, fact, topics, update time, distance. |
| `computed(...)` | Anything derived: counts (`len()` of a response list), the scope dict, `MemoryBankClient.parent`, the retrieval query, the retrieval mode branch, the tool arguments echoed back, and the golden-query evidence blob inside the unlock. |
| `live(...)` | `sessions.create` — the resource name returned by a write this process made. |
| `unmeasured(...)` | Any field the service omitted, plus `card_platform_confirmation` ("no card issuing platform client exists in this process") and `event_count` when `include_events=False`. |
| `static(...)` | **Zero call sites.** Verified by AST. |

`prov.summarize()` on a fully-populated sample payload: 17 SEEDED, 10 COMPUTED, 1 STATIC
(waived, the channel), 0 unwaived STATIC, `clean: true`.

## 6. What I actually verified vs. what is written but unrun

**Verified by execution:**

- `ast.parse()` on the final file — parses clean, Python 3.9 syntax, no `match`.
- All 22 original function names still present; 14 added; none removed or renamed
  (AST diff against the original file).
- Roster extracted from the `list_all_agents()` AST: 8 descriptors, names and order exactly
  matching `tests/test_backend.py` and the README table.
- Guardrail flags present on the single spoke constructor; 7 spokes built through it; hub
  tools and `sub_agents` list confirmed from the AST.
- Zero `except` handlers in the producer layer; zero `static()` calls; one `identity()` call.
- **A stub-client harness** that lifts the producer functions out of the source with `ast`
  and runs them against the real `provenance.py`, the real `field()` helper from
  `memory_bank.py`, and a fake REST client. It confirmed, by running the actual code:
  1. seeded sessions flow through with correct tags, and a session belonging to another
     customer is excluded by the scope filter;
  2. with the corpus emptied, the producer returns `sessions: []`, `memories: []`,
     `session_count: 0`, `memory_count: 0` — no row of any kind;
  3. a client that raises propagates the error out of the producer untouched;
  4. an event missing its timestamp renders `"n/r"`;
  5. the provenance summary is clean with the single waived channel constant.

**Not verified — no way to run it here.** The sandbox has Python 3.9.6 with no flask,
pydantic, pytest, google-cloud SDK, dotenv, certifi or network, so the module cannot be
imported and the test suite cannot run:

- The module has never been imported. Import-time side effects
  (`create_default_memory_bank_client()`, `create_default_catalog_governor()`) are untouched
  from the original, but I did not observe them succeed.
- `AgentDescriptor(...)` construction is unvalidated by pydantic. I matched the declared field
  types (`channel: Optional[BankChannel]`, `tools: List[str]`), but nothing enforced it here.
- `LlmAgent(...)` was never constructed. I am relying on `disallow_transfer_to_parent` /
  `disallow_transfer_to_peers` being accepted kwargs of the installed ADK version, which the
  README states but I could not confirm against the package.
- `/api/agents/list` was never called; `test_agents_list_endpoint` was never run. The
  assertion match was checked statically against the AST, not against a live response.
- No Memory Bank call was ever made. The response shapes I parse (`sessionState.channel`,
  `content.parts[0].text`, `retrievedMemories[].memory.fact`) come from reading
  `memory_bank.py`'s own request/response handling and its CLI printer, not from a live
  payload. **The one substantive risk is the channel token vocabulary**: if the 52 seeded
  sessions label channels in a way `CHANNEL_SESSION_TOKENS` does not cover, producers will
  return empty. First thing to check against the real engine — run
  `python -m backend.memory_bank sessions` and compare the labels.
- BigQuery was never reached, so the new `RuntimeError` path in
  `execute_golden_query_contract` is unexercised.

## 7. Things wrong outside my file (not changed — flagging only)

1. **`backend/models.py` — `AgentId` still enumerates the 6-agent drift**
   (`01_orchestrator` … `06_memory_writer`). The golden query contracts authorize agents by
   these ids, so the substrate and the published roster speak different vocabularies. Either
   add the 8 real agent ids, or keep `AgentId` explicitly as the internal specialist tree's
   vocabulary and document that. I kept the contracts untouched rather than bend them.
2. **`backend/models.py` — `AgentDescriptor` has no `agent_type` field.** The README publishes
   `CHANNEL_PRODUCER` / `PRE_WRITE_VALIDATOR` / `CONSOLIDATED_AUDITOR` / `LEAD_SYNTHESIZER` as
   a column; I had to fold it into `role`. Adding `agent_type: Optional[str]` would make the
   API match the README exactly.
3. **`backend/memory_bank.py` — `CustomerMemoryBank` silently resolves to `None`.** It is
   imported inside `try: from backend.memory_bank_old import CustomerMemoryBank` and that
   module is not in the repo, so the `except Exception:` swallows it and leaves the name bound
   to nothing. Any caller that trusts the README's reference to `CustomerMemoryBank` gets an
   `AttributeError` at runtime, not an import error at startup. Either ship the class or drop
   the name.
4. **`backend/memory_bank.py` seeds `sessionState.channel = "IVR"`** while `models.py` calls
   the same channel `TELEPHONY_IVR`. My alias table absorbs the mismatch, but the seeder
   should really write the `BankChannel` value so the alias table can shrink.
5. **`agents.json` lists only 3 agents** and still points `consumer_credit_synthesizer_agent`
   at `backend/adk_agents.py` with no entry for the five channel producers. It should list 8
   to match the README and the API.
6. **`README.md` roster describes producer payloads as fixed facts** — "declined $142.50
   Target transactions", "NY & Chicago logins", "Apple Pay tokenization rejections". Those are
   now whatever the seeded corpus holds. The roster text should describe what each producer
   *reads*, not what it will *find*, or the docs will contradict a re-seeded engine.
7. **`backend/app.py`** should expect exceptions from golden query tools now that the
   swallowed-BigQuery-error path is gone (see §4), and `/api/agents/list` may want to expose
   the channel enum value rather than the raw model dump.
