# Rework report — `backend/veracity_and_audit.py`

**Scope:** I own and delivered exactly one file, `backend/veracity_and_audit.py`. Nothing else was
edited. Everything I found wrong outside that file is listed at the end as work for its owner.

**Verification status, stated plainly:** this environment is Python 3.9.6 with no flask, no pydantic,
no google SDK and no network. I **could not import the package, could not run it, and could not test
it**. What I verified is:

| Check | Result |
| --- | --- |
| `ast.parse` of the delivered file | **passes** |
| Python 3.9 syntax only (no `match`, no `X \| Y` unions) | **passes** |
| `GroundTruthTelemetryStore` anywhere in the file | **0 occurrences** |
| `KnowledgeCatalog`, `CustomerMemoryBank`, `memory_bank_old`, `knowledge_catalog_old` | **0 occurrences** |
| `api_called`, `text-embedding`, `cosine`, `fallback triggered` | **0 occurrences** |
| `static(` used anywhere | **0 occurrences** |
| All three classes + all three Pydantic schemas still present | **confirmed by AST** |
| Unused imports | **none** |

Everything below the parse line is **written but unrun**. No BigQuery row, no Dataplex aspect and no
Gemini response was ever observed by me.

---

## 1. Why the app could not start, and what I removed

Line 37 of the old file was:

```python
from backend.memory_bank import CustomerMemoryBank, GroundTruthTelemetryStore
```

Both names are re-exported from `backend.memory_bank_old`, which **does not exist in this repo**,
inside a `try/except Exception: pass`. The import silently does nothing, the names never bind, and
`from backend.memory_bank import ...` raises `ImportError` at import time. `backend.app` and
`backend.adk_agents` both import this module, so the whole app was dead on arrival.

The same pattern hit `from backend.knowledge_catalog import KnowledgeCatalog` (line 38) — `KnowledgeCatalog`
comes from the equally absent `backend.knowledge_catalog_old`.

Both imports are gone. The file now imports only names that actually exist:

```python
from backend.knowledge_catalog import CatalogGovernor, create_default_catalog_governor
from backend.memory_bank import MemoryBankClient, create_default_memory_bank_client
from backend.provenance import (NOT_RECORDED, computed, env_param, identity,
                                live, seeded, unmeasured, value_of)
```

---

## 2. Plan item 5 — every `GroundTruthTelemetryStore` call site and what replaced it

| Old line | Old call | Replaced by |
| --- | --- | --- |
| ~131 | `GroundTruthTelemetryStore.fetch_live_account_statement(customer_id)` | `golden_01_customer_360_snapshot(customer_id=...)` for the customer/card record, and `golden_02_recent_authorizations_for_card(card_id=...)` for the ledger itself. Source tags `bigquery:v_customer_360#golden_01`, `bigquery:cards#golden_01`, `bigquery:authorizations#golden_02`. |
| ~132 | `GroundTruthTelemetryStore.fetch_travel_and_geo_verification(customer_id)` | Avenue `AVENUE_TRAVEL_GEO`, table `travel_notices`, resolved through the contract registry. **No golden contract currently covers `travel_notices`** (see §5), so the avenue records `unmeasured("no golden query contract governs travel_notices; avenue not measured")` → renders `"n/r"`. It does not invent a travel notice. |
| ~133 | `GroundTruthTelemetryStore.fetch_step_up_consent_audit(customer_id)` | Deleted outright. There is no SMS-consent table in the 12-table dataset; the whole "Multi-Step SMS Y/N Consent Audit / eSIM clone" avenue was fiction built on a dict. Nothing replaces it, and nothing pretends to. |
| ~134 | `GroundTruthTelemetryStore.get_telemetry_for_customer(customer_id)` → `telephony_ivr_logs`, `mobile_app_logs` | Avenue `AVENUE_CONTACT_HISTORY`, table `contact_events`. Same story as travel: **no contract covers it today**, so it records `unmeasured(...)` rather than a fabricated IVR log. `mobile_app_logs` has no table in the dataset and is gone. |
| ~136 | `KnowledgeCatalog.get_customer_risk_profile` / `KnowledgeCatalog.query_policies` | Gone. The Dataplex client now supplies governance (`CatalogGovernor.check_table` / `check_column`); it is not used to manufacture a "risk profile" or a "behavioural baseline". |
| ~227-260 | The `avenue_1..avenue_4` return dict with `"api_called": "GroundTruthTelemetryStore.fetch_live_account_statement"` and the `text-embedding-005 cosine similarity` label | The whole dict is replaced by `build_evidence()`. Every block now names the real wrapper it called (`"query": computed("golden_02_recent_authorizations_for_card", ...)`), the real parameters bound, the real row count, and the rows themselves, each tagged `seeded(v, "bigquery:<table>#golden_<id>")`. There is no `api_called` key and no embedding/similarity claim anywhere — the file does no local embeddings and no cosine maths, and says so. |
| ~372, ~405 | `_algorithmic_telemetry_evaluation` — ~210 lines of `if "lisbon" in claim_lower`, `if "1000" in claim_lower`, returning hand-written verdicts with hand-written IP addresses, timestamps and confidence scores | **Deleted in full.** A keyword match that returns `confidence_score=0.96` and a fabricated session record is not a validator. The verdict now comes from one Gemini structured-output call over real rows. |
| ~537 | `logging.warning("Vertex AI Gemini live evaluation fallback triggered")` + fabricated verdict behind it | Replaced with `raise VeracityServiceError(...)`. If Vertex is unreachable, or returns nothing parsable, the call fails loudly and says which customer and which error. There is no path in this file that produces a verdict without a model response over real rows. |
| ~670 | The audit agent's `GroundTruthTelemetryStore` cross-check | Replaced by `_contradictions_against_bigquery()`, which arbitrates every amount and dispute id in memory against `golden_07_open_disputes_for_customer` (`bigquery:disputes#golden_07`). |

Also deleted: the ~440-line prompt block of scenario instructions ("If the claim is about London
Heathrow Duty Free… DO NOT mention the Chicago $1,000 charge"). That prompt was steering the model
to a predetermined story. The new prompt contains no scenario, no city, no amount and no merchant —
only rules about how to treat the evidence it is given.

---

## 3. The golden-query layer — exact functions and signatures called

Called by name, with the signatures as they exist in `backend/adk_agents.py`:

```python
golden_01_customer_360_snapshot(customer_id: str) -> Dict[str, Any]
golden_02_recent_authorizations_for_card(card_id: str, lookback_days: int = 14, row_limit: int = 25) -> Dict[str, Any]
golden_07_open_disputes_for_customer(customer_id: str) -> Dict[str, Any]
golden_10_wallet_tokens_active_on_card(card_id: str) -> Dict[str, Any]
```

Plus the contract layer itself:

```python
GOLDEN_QUERY_CONTRACTS          # read to resolve avenue table -> contract
contract.tool_name, contract.query_id, contract.governed_tables
```

Three design decisions worth stating:

**a) I call the `golden_*` wrappers, not `execute_golden_query_contract` directly.** Each wrapper
declares its own `calling_agent` (`FRAUD_TRIAGE` for 02, `RESOLUTION_ACTIONS` for 10,
`POLICY_COMPLIANCE` for 07, `IDENTITY_CONTEXT` for 01). The validator is agent 6 of an 8-agent
roster and **is not in the `AgentId` enum at all**, so calling `execute_golden_query_contract`
directly with any agent id I picked would either be a lie or an immediate `allowed: False`. Borrowing
the wrapper's declared identity is honest and uses the authorization that `INDEX.md` actually grants.
See §7 for the follow-up this implies.

**b) Avenue → contract is resolved from the registry at call time, not hardcoded.**
`_contract_for_table(table)` scans `GOLDEN_QUERY_CONTRACTS` for the contract whose *primary*
governed table matches. So when whoever owns the query pack adds a `travel_notices` or
`contact_events` contract, those two avenues start returning real rows with no edit to my file.

**c) The contract layer's swallowed BigQuery error is caught and refused.**
`execute_golden_query_contract` wraps its BigQuery call in
`except Exception: rows = [{"note": f"BigQuery execution note: {exc}"}]`. That returns `row_count: 1`
and a healthy-looking result for a query that never ran. `_rows_or_raise()` detects that exact row
shape (a dict whose only keys are `note`/`error`) and raises `VeracityServiceError` naming the
underlying failure. It also raises on `allowed: False` and on a missing `rows` key.

**Every query is parameterised.** The card id is never a literal: `_resolve_cards()` derives it from
`golden_01`'s rows, and a card id mentioned in the claim text is preferred only if `golden_01`
confirms the customer actually holds it. If no card resolves, the card-scoped avenues record
`unmeasured("no card id resolved for <customer>")` rather than falling back to `CARD00002`.

---

## 4. Plan item 6 — the catalog authorization gate

`ClaimVeracityValidatorAgent.authorize_sources()` runs **before any table is read**, and
`build_evidence()` calls it first. Its output drives what is read; it is not a log written after the
fact.

### How it works

Each avenue declares *candidate* tables. The gate asks `CatalogGovernor.check_table(table, dataset)`
about every candidate and keeps only the ones that come back `ALLOW`:

```python
{"avenue_id": "AVENUE_AUTH_LEDGER",
 "candidate_tables": ("authorizations", "authorizations_deprecated_v1"), ...}
```

Then it probes the governed columns with `CatalogGovernor.check_column(table, column, dataset)`:

```python
RESTRICTED_COLUMN_PROBES = (("customers", "ssn_last4"),
                            ("cards", "pan_full"),
                            ("cards", "pan_masked"))
```

### Where the deprecated-table trap fires

In `_authorization_record()`, on the candidate `authorizations_deprecated_v1`.
`CatalogGovernor.check_table` reads the entry's `lifecycle-status` aspect and returns
`allowed=False, action="REFUSE", evidence="lifecycle-status.status = DEPRECATED"`, plus
`replacement` from the `authoritative_replacement` field.

**The code does not know which of the two candidates is deprecated.** Both are offered to the
catalog in the same loop, with the same call. No string in my file matches `"deprecated"`, and
nothing branches on the table name. If somebody flipped the aspect in Dataplex so that
`authorizations` was the deprecated one, the agent would decline `authorizations` instead — which is
the whole point of the demo. The refusal is recorded with the aspect text it came from, and the
replacement the catalog named is carried through so the trace can show *what it should use instead*.

`pan_masked` is in the probe list for the same reason: it is expected to come back `ALLOW`, which
proves the gate is reading answers rather than refusing everything it asks about.

### Where the REFUSE-column trap fires

In `_column_records()`, on `customers.ssn_last4` and `cards.pan_full`. `check_column` acts on the
`sensitivity-class.agent_behavior` aspect (never on the human-facing `level` label) and returns
`REFUSE`. Those subjects go into `columns_declined`.

Declining is not only a log line. `_redact(rows, declined_columns)` runs over **every** result set
before it is tagged and emitted, blanking any declined column name that appears in a row and
replacing the value with the literal `"n/r"`. The golden SQL is not supposed to select those columns,
but if it ever did, the decline would still hold — otherwise the evidence record would claim a
refusal that the output contradicts.

### What is never claimed

Nothing in the file says or implies Dataplex blocked anything. The constant emitted with every
record is:

> "Dataplex advises; it does not enforce. The catalog supplied the basis and the agent's decision to
> decline is the control being demonstrated."

---

## 5. The evidence record emitted per table read

For each table, `_authorization_record()` emits exactly the three things the brief asks for — what
was asked, what the catalog said, whether the agent honoured it:

```python
{
  "avenue_id": "AVENUE_AUTH_LEDGER",
  "table": "authorizations_deprecated_v1",
  "asked": "May this validator read authorizations_deprecated_v1 for AVENUE_AUTH_LEDGER?",
  "catalog_said": live({...Decision.to_dict()...},
                       "dataplex:entries.lookup:projects/<p>/locations/<l>/entryGroups/@bigquery/entries/.../tables/authorizations_deprecated_v1"),
  "aspect_cited": live("lifecycle-status.status = DEPRECATED", "dataplex:entries.lookup:<entry>"),
  "authoritative_replacement": live("authorizations", "dataplex:entries.lookup:<entry>"),
  "agent_action": computed("DECLINED", "validator.catalog_gate"),
  "agent_honoured_catalog": computed(True, "validator.catalog_gate"),
  "enforcement": "Dataplex advises; it does not enforce. ...",
}
```

Column records are the same shape plus `regulation` (from `regulation_reference`). The gate's summary
adds `tables_declined`, `columns_declined` and `allowed_tables_by_avenue`. All of it lands in
`ClaimVeracityEvaluation.multi_avenue_audit` under `catalog_authorization`, so the UI trace can
render the refusal and its basis without a second call.

Data blocks alongside it, per avenue:

```python
{"avenue_id": ..., "avenue_title": ..., "table": "authorizations",
 "query": computed("golden_02_recent_authorizations_for_card", "validator.evidence"),
 "parameters": computed({"card_id": "<resolved>"}, "validator.evidence"),
 "row_count": seeded(14, "bigquery:authorizations#golden_02"),
 "rows": seeded([...], "bigquery:authorizations#golden_02")}
```

An avenue that was declined, unresolvable or uncovered by any contract carries `unmeasured(...)` in
place of `rows` and `row_count`, with the reason in the source string. The model prompt states
explicitly that an `"n/r"` avenue is **not** evidence for the claim — an unread source is not a clean
bill of health.

---

## 6. Provenance tags used

| Tag | Where | Example source |
| --- | --- | --- |
| `seeded` | every BigQuery row, row count, resolved card id | `bigquery:authorizations#golden_02`, `bigquery:disputes#golden_07`, `bigquery:cards#golden_01` |
| `live` | every Dataplex aspect read — table verdicts, column verdicts, cited aspect text, replacement, regulation | `dataplex:entries.lookup:projects/…/tables/authorizations_deprecated_v1` |
| `computed` | agent decisions and in-process derivations: `agent_action`, `agent_honoured_catalog`, the amount cross-check, the declined lists, the veracity status copied into the case evidence | `validator.catalog_gate`, `validator.cross_check`, `validator.amount_cross_check`, `validator.evidence` |
| `unmeasured` | anything not measured — uncovered avenues, declined tables, unresolved card ids, un-run verification | `"no golden query contract governs travel_notices; avenue not measured"` |
| `identity` | **one** use: `"gate": identity("catalog-before-read", "validator.catalog_gate", …)` — the name of the gate this agent implements | — |
| `static` | **never used.** Not imported. | — |

Fixed prose (the Dataplex advisory note, the amount-arbitration rule) is emitted as a plain untagged
string rather than abusing `identity()` on something that is not an identity.

**Amounts, deadlines, dispute ids.** `_amount_cross_check()` pulls every figure the claim quotes,
compares it to every amount-like column in the authorised result sets, and emits `match_by_amount`
per figure. The prompt requires the model to state that result. The audit agent enforces the same
rule from the other side: any amount or `DSP#####` in a stored memory that the disputes table does
not confirm becomes a `CRITICAL` `MEMORY_FIGURE_NOT_IN_BIGQUERY` anomaly.

**No hardcoded ids.** `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `BQ_DATASET`,
`VERACITY_MODEL` and `AUDIT_CUSTOMER_IDS` are all read through `env_param`, each also overridable as
a constructor argument. `GOOGLE_CLOUD_PROJECT` has **no** default: if it is unset, building the
Vertex client raises rather than pointing at `your-gcp-project-id` and producing a run that looks
healthy while talking to nothing.

---

## 7. What else changed inside my file

**`MemoryBankAuditAgent`** no longer uses the non-existent `CustomerMemoryBank`. It reads through the
real `MemoryBankClient.retrieve(scope={"customer_id": ...}, top_k=...)` — service-side semantic
retrieval, no client-side embedding or consolidation. Two passes: a deterministic BigQuery
arbitration of every figure, then a Gemini pass for contradictions *between* memories (explicitly
told not to judge figures, since the first pass owns that).

`run_consolidated_sweep()` has **no default customer roster**. `KNOWN_CUSTOMERS` was another
`memory_bank_old` import. It now takes `customer_ids`, falls back to `AUDIT_CUSTOMER_IDS`, and raises
if neither is set. Putting invented customer ids in an audit report is the opposite of an audit.

**`AdminCardOversightManager`** was rebuilt. The old `get_or_init_case` was a single ~2,000-character
literal: a named customer, a card last-4, two euro amounts, dispute ids, a chat transcript, four
verification checks and a `0.98` confidence score — all hand-written. It also constructed
`AdminCardReviewCase` and `VerificationCheckItem` with **field names that do not exist in
`backend/models.py`**, so it would have raised a pydantic `ValidationError` on first call even if the
imports had worked. The case is now built from `golden_07` rows against the real schema
(`card_name`, `flagged_transaction`, `flagged_amount`, `verification_passed`,
`chat_verification_transcript: List[Dict[str, str]]`, `recommended_admin_decision`), with `"n/r"`
wherever a row field is absent.

One honest wrinkle: `AdminCardReviewCase.confidence_score` is a required `float`, so a case that has
not been verified yet cannot literally hold `"n/r"`. It holds `0.0` — zero corroborations, which is a
true statement of the evidence — and the parallel evidence record carries
`unmeasured("no verification checks executed yet")` so nothing reads `0.0` as a measured score. Once
verification runs, the score is the validator's own confidence. **If you would rather the field were
`Optional[float]`, that is a `models.py` change and I did not make it.**

`evaluate_customer_chat_verification` keeps its `scenario` parameter because `app.py` still passes it,
but the parameter **cannot affect the outcome**. It is logged as a hint that was not honoured and
recorded as `{"supplied": ..., "honoured": False}`. The verdict comes only from the validator running
over BigQuery evidence.

`execute_admin_decision` rejects any decision that is not `APPROVED_YES` or `REJECTED_NO` instead of
silently treating an unknown string as a denial.

A new read-only classmethod `case_evidence(customer_id)` exposes the provenance-tagged record behind
a case for the trace drawer. It adds nothing to the existing surface and nothing depends on it.

---

## 8. Things wrong outside my file — for their owners

**`backend/memory_bank.py` (memory-bank owner) — blocking.**
The `try: from backend.memory_bank_old import (CustomerMemoryBank, GroundTruthTelemetryStore,
KNOWN_CUSTOMERS, get_or_create_reasoning_engine_id) except Exception: pass` block at the end
re-exports four names from a module that does not exist. Any `from backend.memory_bank import
CustomerMemoryBank` fails. `backend/app.py` still does this (e.g. around line 972). Either the block
or every consumer has to go.

**`backend/knowledge_catalog.py` (catalog owner) — blocking.**
Identical pattern: `from backend.knowledge_catalog_old import (KnowledgeCatalog,
AWSOnPremHybridConnector)` inside a swallowing `try/except`.

**`backend/adk_agents.py` (adk owner) — three items.**

1. *Silent BigQuery failure.* `execute_golden_query_contract` turns any exception into
   `rows = [{"note": f"BigQuery execution note: {exc}"}]` and still returns `row_count: 1` and a
   catalog verdict. Every caller that does not specifically detect that row shape will treat a failed
   query as a successful empty-ish result. I defend against it in my file; it should not exist in
   the first place. Suggest re-raising, or returning `{"allowed": False, "error": ...}`.
2. *Hardcoded project default.* `bigquery.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT",
   "abhinayboda-genai-bb"))` — a personal project id as a fallback.
3. *Hardcoded `as_of_ts` / `as_of_date`.* The `golden_*` wrappers pin `datetime(2026, 9, 22, 12, 0)`
   and `date(2026, 9, 22)` in source. Those should be parameters with an env default.

**`backend/queries/golden/` (query owner) — blocking, and it caps what my file can prove.**
The directory **does not exist**. There is no `INDEX.md` and none of the twelve `.sql` files, so
`load_golden_sql_text()` raises `FileNotFoundError` on every call and not a single golden query can
run today. On top of that, **no registered contract covers `travel_notices` or `contact_events`** —
two of the four telemetry avenues the plan names. The registry's twelve contracts cover
`v_customer_360/customers/accounts/cards`, `authorizations`, `agg_merchant_velocity_daily`,
`disputes`, `wallet_tokens/devices`, `dispute_events` and `merchants`. Until contracts (say `13` and
`14`) are added for those two tables, my validator will honestly report both avenues as `"n/r"`
rather than invent them. It will pick them up automatically once they are registered — no edit to my
file required.

**`queries/golden/INDEX.md` authorization (query owner) — design question, not a bug.**
The validator is agent 6 of 8 and does not appear in the `AgentId` enum, which has six values. I
therefore call the `golden_*` wrappers and inherit their declared `calling_agent`. If the team wants
the validator to be a first-class caller, add it to `AgentId` and to the `authorized_agents` of
contracts `01`, `02`, `07` and `10`; I would then call `execute_golden_query_contract` directly with
its own identity. That is a one-line change in my file once the enum exists.

**`backend/models.py` (models owner) — two optional items.**
`AdminCardReviewCase.confidence_score: float` is required, which is why an un-verified case shows
`0.0` (see §7). `Optional[float] = None` would let it render `"n/r"` properly. Also,
`AdminCardReviewCase` has no `admin_decision_reason` or `admin_decision_timestamp` field, but the old
code set both — check whether the frontend expects them.

**`backend/app.py` (app owner) — check after this lands.**
`app.py` imports `CustomerMemoryBank` (dead, above), and around lines 500-580 it hand-builds
`relevant_avenues_checked` dicts with literal findings ("CORROBORATED: `SHP00001` is in `IN_TRANSIT`
state…") — the same fabrication pattern I removed from my file. It also derives
`scenario="PASSED"/"FAILED"` from a veracity status and passes it to
`evaluate_customer_chat_verification`; that argument is now ignored by design, so the call still
works but the branch upstream is dead weight. My public method names and signatures are unchanged, so
nothing in `app.py` breaks on signature grounds.

**`README.md` (docs owner).**
Line 44 still documents `GroundTruthTelemetryStore - IPs, IVR, POS, Travel Notices` as the
validator's ground-truth source. That component no longer exists.
