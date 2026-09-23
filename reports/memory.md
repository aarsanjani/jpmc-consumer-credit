# Memory layer — what I changed and what I proved

Scope: `backend/memory_bank.py`, `backend/knowledge_catalog.py` (minimal surgery), and
three new files under `scripts/`. I did not touch `app.py`, `agent.py`, `adk_agents.py`,
`veracity_and_audit.py`, `models.py`, the frontend or the README — things they need are
in the last section.

**Headline:** the app could not start at all, and the reason was hidden by a
`try/except Exception: pass`. That is fixed. Memory is now seeded as nine discrete facts
instead of one paragraph, and no memory can carry an amount, a deadline or a dispute id —
enforced at write time by a guard I ran, not by a convention I wrote down.

---

## 1. Item 7 — the `_old` bridges are gone, and what they were hiding

Both files ended with a "Backward Compatibility Bridge":

```python
try:
    from backend.memory_bank_old import (
        CustomerMemoryBank, GroundTruthTelemetryStore,
        KNOWN_CUSTOMERS, get_or_create_reasoning_engine_id,
    )
except Exception:  # pragma: no cover
    pass
```

`backend/memory_bank_old.py` and `backend/knowledge_catalog_old.py` do not exist anywhere
in the repo. So the import always failed, the bare `except` always swallowed it, and the
four names were never defined. `app.py`, `agent.py` and `veracity_and_audit.py` all do
`from backend.memory_bank import GroundTruthTelemetryStore`, which therefore raised
ImportError at startup. The app could not boot.

The `except Exception: pass` is the whole defect. It turned a missing module into silence,
so the failure surfaced three files away as a confusing name error instead of at the line
that caused it.

**Verified by running it.** Against the untouched source repo:

```
ImportError: cannot import name 'GroundTruthTelemetryStore' from 'backend.memory_bank'
```

Against my files, after removing both bridge blocks and the stale section headings that
named them:

```
memory_bank imports clean. create_memory: True | delete_memory: True
knowledge_catalog imports clean
```

I deleted no files, added no replacement shim, and did not re-export
`GroundTruthTelemetryStore` — other workers are removing its call sites. Everything else
in both files is byte-identical to the source except the two additions below. The full
diff of `knowledge_catalog.py` is nine deleted lines and one heading.

### The one addition to `memory_bank.py`

The client had `generate_from_session`, `generate_from_contents`, `retrieve` and
`list_memories` — no way to write a single known fact and no way to remove one. Item 2
requires both (`memories.create` per fact, plus `--purge-blob`). Rather than hand-roll
HTTP in the script, I added two thin methods that reuse the client's existing `_request`:

- `create_memory(fact, scope)` → `POST {base}/memories`
- `delete_memory(memory_name)` → `DELETE {base}/memories/{id}`

Both match the documented Vertex AI Agent Engine Memory Bank REST surface
(`projects.locations.reasoningEngines.memories.create` / `.delete`, v1beta1). 37 added
lines, no existing line changed, no restructuring. `delete_memory` requires the full
resource name the service returned rather than a bare id, so a caller cannot delete in the
wrong engine by pasting a fragment.

Why `create` and not `generate`: `memories:generate` asks Gemini to decide what is worth
remembering from a conversation. For seeding, the operator has already decided, and the
fact is already atomic — routing it through the extractor lets it be re-worded or merged,
which is precisely how a paragraph gets made.

---

## 2. Item 2 — memory shape

### What was wrong

A local "Dreaming Service" compaction step wrote one ~200-word narrative into Memory Bank.
That breaks retrieval twice:

- **Semantic search has one candidate.** "Where is the customer", "what did they ask us not
  to do" and "which cards were lost" all return the same paragraph. Relevance scoring over
  a set of one is meaningless.
- **Consolidation has nothing to supersede.** Memory Bank resolves contradictions by
  replacing a fact with a newer fact. It cannot replace one clause inside a paragraph, so a
  stale clause — "travelling until the twelfth" — outlives the trip forever.

Nothing about compaction survives in the two files I own. `memory_bank.py` already said in
its own header that it does no local embeddings, no cosine similarity and no compaction;
that is still true, and I added nothing that contradicts it. There is no
`DynamicCompactionSchema`, no token accounting, no client-side consolidation and no local
ranking in anything I deliver. (The compaction API, the `CompactionReport` model and the
frontend button live in files other workers own — see section 5.)

### The nine atomic facts for CUST00001

Every one is a restatement of a single turn in `backend.memory_bank.LISBON_THEFT_TURNS`,
the 09 Sep IVR call that is the seeded scenario. Nothing is inferred beyond that
transcript. Each carries a `why_this_grain` note in the script, printed by `--dry-run`.

| # | Fact | Why this grain |
|---|------|----------------|
| 1 | `Travelling in Lisbon 03-12 Sep` | Where the customer is, as a trip window. No year: a memory that dates itself goes stale, and the year is on the travel-notice row. This is what stops a Lisbon charge reading as automatically suspicious. |
| 2 | `Reported the wallet stolen while in Lisbon` | The event, with no amount and no dispute id attached. Separate from the trip fact so a later "trip extended" memory supersedes the trip without touching the theft. |
| 3 | `Both the debit card and the credit card were in the stolen wallet` | Scope of the loss in words. Naming CARD00002 / CARD00004 would pin the memory to rows that get reissued; the card table answers "which cards" at use time. |
| 4 | `Asked for both cards to be frozen straight away` | The customer's instruction, not the system's action. Whether the freeze is currently in place is card state in BigQuery; what the customer asked for is durable. |
| 5 | `Disputes the Lisbon charges made after the wallet was taken` | The load-bearing example of the rule: the dispute exists, and the memory says nothing about how much or which one. |
| 6 | `Needs a working card for the rest of the trip` | A standing need phrased relative to the trip, not to a date, so it stays true if the return flight moves. |
| 7 | `Accepted a virtual card as the immediate stand-in` | A decision the customer agreed to. Discrete, so a later refusal can supersede this one fact. |
| 8 | `Replacement card goes to the hotel, not the home address` | A delivery preference that contradicts the address on file — exactly what consolidation needs as its own unit. When the trip ends, "send to home" supersedes this and nothing else. |
| 9 | `Notify before closing any dispute` | A durable instruction that outlives this case. Buried in a paragraph it is invisible to a query about contact preferences; on its own it retrieves for "what has this customer asked us to do". |

Two things I deliberately did **not** seed, because they are state rather than memory and
BigQuery already owns them: the wallet-token revocation (golden query 10 answers "which
tokens are active"), and the current freeze status of each card.

### The script

`scripts/seed_atomic_memories.py`

- One `MemoryBankClient.create_memory(fact, scope)` call per fact — nine calls, never a
  batch and never a paragraph. It calls the client in `backend/memory_bank.py`; it does not
  reimplement any part of it.
- `scope={"customer_id": customer_id}` is built in `main()` and passed on every call. The
  client stays engine-level, as its own docstring insists, so one instance serves every
  customer with no cross-contamination.
- `create` returns a long-running operation, so each write goes through the client's
  existing `wait_for_operation`. Without that, a fast loop followed by a retrieve looks
  like the writes silently failed.
- No hardcoded project, engine or location. Each is an argparse argument defaulting to an
  environment variable (`GOOGLE_CLOUD_PROJECT`, `VERTEX_AGENT_ENGINE_ID` /
  `AGENT_ENGINE_ID` / `VERTEX_MEMORY_BANK_ENGINE_ID`, `GOOGLE_CLOUD_LOCATION`,
  `VERTEX_API_VERSION`). Unsupplied ids render as `n/r` in the dry run and **stop** a live
  run — they are never defaulted to a literal.
- Provenance: the plan is tagged with `seeded`/`computed`/`unmeasured` from
  `backend.provenance` and the run prints its own tag counts. `STATIC_UNWAIVED` is 0.

### How the blob is purged

`--purge-blob` does this, in order:

1. `client.retrieve(scope)` — a scoped read, so nothing outside this customer is ever a
   candidate.
2. For each memory, decide if it is the blob on two independent signals: **word count** at
   or above `--blob-min-words` (default 40 — a memory that long is a narrative, not a
   fact), or a **compaction marker** substring (`compaction`, `compacted`, `summary of`,
   `dreaming`, plus anything added with repeatable `--blob-contains`).
3. Print the resource name, the reason and the first 200 characters of every candidate
   **before** deleting it, then `client.delete_memory(name)`.
4. Non-candidates are printed as `keep`, annotated if they violate the
   no-authoritative-values rule, so a partial cleanup is visible rather than silent.

Safety rails: a live purge refuses to run without `--yes`; if the service returns a memory
with no resource name the run **stops with an error** rather than guessing an id; and if
the scope comes back empty it says so explicitly instead of reporting a successful purge of
nothing. `--dry-run` prints the retrieve and the DELETE template and touches nothing.

---

## 3. Item 3 — scenario alignment, BigQuery wins

### The reconciliation

The stored memory asserted **EUR 412**, **90** and **1480**. The authoritative BigQuery
disputes are **USD 1,284.60** and **USD 3,940.15**. Every memory-side number is wrong, and
two of them are wrong in the currency as well as the amount:

| Memory asserted | BigQuery (authoritative) | Verdict |
|---|---|---|
| EUR 412 | USD 1,284.60 / USD 3,940.15 | **MISMATCH** — wrong amount and wrong currency |
| EUR 1480 | USD 1,284.60 / USD 3,940.15 | **MISMATCH** — wrong amount and wrong currency |
| 90 (days) | 90-day investigation window | agrees today, **still the wrong home** |

The third row is the interesting one. "90" happens to be right, and it is still a defect:
the window is policy plus the filing date, both looked up, so a memory holding it is right
only until either changes — and nothing marks it stale when they do. The reconciler reports
that as `MATCHES-BUT-STILL-WRONG-HOME` and exits non-zero on it, because "the number
agreed" is not the standard; "the number is not in memory" is.

The nine replacement facts assert none of these. The dispute is described
("disputes the Lisbon charges made after the wallet was taken") and the amount, the
currency, the dispute id and the deadline are read from BigQuery at use time.

### How the rule is enforced — three layers, not a convention

**(a) Write time — `scripts/memory_facts.py`.** `assert_no_authoritative_values(text)`
raises `AuthoritativeValueError` unless the string is safe to store. Stdlib only, no
pydantic, no I/O, so it runs anywhere. Eight rules, ordered most-specific first so the
message names the right thing:

| Rule | Catches |
|---|---|
| `currency_amount` | `EUR 412`, `$1,284.60`, `412 EUR`, `€620` |
| `monetary_shaped_number` | `1,284.60`, `3,940.15`, `412.00` — grouping or cents means somebody meant money |
| `dispute_id` | `DSP00034`, `CASE-1234`, `CLM 998` |
| `record_id` | any `CUST00001` / `CARD00002` shape — a memory that names a row is stale when the row moves |
| `dated_deadline` | `2026-09-23`, `23 Sep 2026`, `09/09/2026` — a date carrying a year |
| `deadline_phrase` | `due …`, `deadline …`, `no later than …`, `expires …` near a date or weekday |
| `duration_window` | `90 calendar days`, `2 billing cycles`, `10 business days` |
| `bare_quantity` | any run of 3+ digits, e.g. `1480` — no currency marker, still read as an amount |

The failure message names the matched text, the rule, the character offsets and *why that
value belongs in BigQuery* — different reasons for amounts, deadlines and ids, because the
reason is what stops the next person re-adding it.

Two deliberate calls. It is stricter than asked on ids: any record id, not only dispute
ids. And two-digit numbers pass, which is what lets `03-12 Sep` through while `412` and
`1480` are rejected.

**(b) In the seeder.** `check_all_facts()` runs the guard over all nine facts before
anything is written or even printed, and aborts the whole run on the first bad one rather
than writing the good ones first — a half-seeded scope is harder to reason about than an
empty one. Each fact passes through `assert_no_authoritative_values` again immediately
before its `create_memory` call.

**(c) After the fact — `scripts/reconcile_scenario.py`.** Reads the disputes from BigQuery
through the existing golden-query layer (`golden_07_open_disputes_for_customer`, then
`golden_08_dispute_filing_window_check` for each dispute id **that 07 returned** — no
dispute id is typed into the script), reuses the same guard to find every value memory
asserts, and prints the comparison. Exit 1 on a mismatch, exit 3 on a rule violation with
no mismatch or on an unverifiable claim, 0 only when memory asserts nothing authoritative.

Column names are resolved from a candidate list per field and the matched column is printed;
if no candidate is present the value renders `n/r` and the comparison is reported
`UNVERIFIABLE`, never scored as a pass. That is because `backend/queries/golden/*.sql` is
absent from this repo copy, so I could not confirm the real schema and refused to bake in a
guess.

`--dry-run` prints the exact contracts, tools, parameters and authorised agent it would
use, and leaves every authoritative value as `n/r`. **It does not print USD 1,284.60 from a
literal.** The script never hardcodes the answer; if you have a captured result, pass it
with `--bq-fixture` and the comparison runs for real with no GCP access.

---

## 4. Real terminal output — what I actually ran

Environment: Python 3.9.6, no flask, no pydantic, no pytest, no Google SDK, no network.
Everything below is pasted from the terminal, not reconstructed.

### 4.1 The guard, against the bad strings and the good facts

`python3 scripts/memory_facts.py` → exit 0

```
==========================================================================
MUST REJECT - strings the pre-fix memory blob asserted
==========================================================================
  REJECTED  'EUR 412'                                                      rule=currency_amount matched='EUR 412'
  REJECTED  '1480'                                                         rule=bare_quantity matched='1480'
  REJECTED  'Customer disputes EUR 412 at Casa Verde Electronics in Lisbon' rule=currency_amount matched='EUR 412'
  REJECTED  'Disputed credit card total EUR 1,480.00'                      rule=currency_amount matched='EUR 1,480.00'
  REJECTED  'USD 1,284.60 dispute opened on the debit card'                rule=currency_amount matched='USD 1,284.60'
  REJECTED  'DSP00034'                                                     rule=dispute_id matched='DSP00034'
  REJECTED  'Dispute DSP00033 is running under Regulation E'               rule=dispute_id matched='DSP00033'
  REJECTED  'Provisional credit due 23 Sep 2026'                           rule=dated_deadline matched='23 Sep 2026'
  REJECTED  'Foreign-initiated investigation window is 90 calendar days'   rule=duration_window matched='90 calendar days'
  REJECTED  'Resolve within 2 billing cycles'                              rule=duration_window matched='2 billing cycles'
  REJECTED  'Primary card on file is CARD00002'                            rule=record_id matched='CARD00002'
  REJECTED  'Customer CUST00001 reported a stolen wallet'                  rule=record_id matched='CUST00001'

==========================================================================
MUST ACCEPT - atomic facts at the right grain
==========================================================================
  ACCEPTED  'Travelling in Lisbon 03-12 Sep'
  ACCEPTED  'Notify before closing any dispute'
  ACCEPTED  'Reported the wallet stolen while in Lisbon'
  ACCEPTED  'Both the debit card and the credit card were in the stolen wallet'
  ACCEPTED  'Asked for both cards to be frozen straight away'
  ACCEPTED  'Disputes the Lisbon charges made after the wallet was taken'
  ACCEPTED  'Needs a working card for the rest of the trip'
  ACCEPTED  'Accepted a virtual card as the immediate stand-in'
  ACCEPTED  'Replacement card goes to the hotel, not the home address'

==========================================================================
FULL REJECTION MESSAGE - what the seeding script prints and refuses on
==========================================================================
memory rejected - this fact carries a value that BigQuery owns.
  fact    : 'Customer disputes EUR 412 and EUR 1,480.00 on DSP00034; provisional credit due 23 Sep 2026 within 90 calendar days.'
  hit 1   : 'EUR 412'  [rule: currency_amount, at chars 18:25]
            why: an amount belongs to the disputes/authorizations tables in BigQuery, which are read at use time. A memory holding an amount becomes a second source of truth that nothing updates when the ledger changes.
  hit 2   : 'EUR 1,480.00'  [rule: currency_amount, at chars 30:42]
            why: an amount belongs to the disputes/authorizations tables in BigQuery, which are read at use time. A memory holding an amount becomes a second source of truth that nothing updates when the ledger changes.
  hit 3   : 'DSP00034'  [rule: dispute_id, at chars 46:54]
            why: a dispute id identifies a row that BigQuery owns and can reassign when a case is split or merged. Say what the dispute is about; let the query supply the id.
  hit 4   : '23 Sep 2026'  [rule: dated_deadline, at chars 79:90]
            why: a regulatory deadline is derived from the filing date in BigQuery every time it is needed. A memory holding a date keeps quoting it after the case moves, and a wrong Reg E date is a compliance defect, not a wording defect.
  hit 5   : '90 calendar days'  [rule: duration_window, at chars 98:114]
            why: an investigation or provisional-credit window is policy plus the filing date, both of which are looked up. A memory asserting a number of days will contradict the rulebook the first time the policy or the filing date changes.
  fix     : state the situation in words and let the reader fetch the number, date or id from BigQuery at use time.

result: 0 failure(s)
```

Twelve rejects, nine accepts, no false positives, no leaks.

### 4.2 The seeder's dry run, with ids supplied from the environment

```
GOOGLE_CLOUD_PROJECT=example-project VERTEX_AGENT_ENGINE_ID=1234567890 \
GOOGLE_CLOUD_LOCATION=us-central1 VERTEX_API_VERSION=v1beta1 \
python3 scripts/seed_atomic_memories.py --dry-run --purge-blob
```

```
DRY RUN - nothing is sent, no credential is read.
Unsupplied ids render as 'n/r'. They are not defaulted to a literal, because a plausible-looking project id is how a run talks to nothing and still looks healthy.

scope passed on every call : {"customer_id": "CUST00001"}
engine base url            : https://us-central1-aiplatform.googleapis.com/v1beta1/projects/example-project/locations/us-central1/reasoningEngines/1234567890
client method per fact     : MemoryBankClient.create_memory(fact, scope)

--------------------------------------------------------------------------
STEP 1 - purge the pre-fix narrative blob
--------------------------------------------------------------------------
  POST https://us-central1-aiplatform.googleapis.com/v1beta1/projects/example-project/locations/us-central1/reasoningEngines/1234567890/memories:retrieve
       body: {"scope": {"customer_id": "CUST00001"}}
  then, for each returned memory that looks like the blob
       (>= 40 words, or containing one of ['compaction', 'compacted', 'summary of', 'dreaming']):
  DELETE https://us-central1-aiplatform.googleapis.com/v1beta1/projects/example-project/locations/us-central1/reasoningEngines/1234567890/memories/{memory_id}
  A live run refuses to delete without --yes. Nothing is deleted by word count alone without printing the memory first.

--------------------------------------------------------------------------
STEP 2 - write 9 atomic memories, one call each
--------------------------------------------------------------------------
  [1/9] POST https://us-central1-aiplatform.googleapis.com/v1beta1/projects/example-project/locations/us-central1/reasoningEngines/1234567890/memories
         body: {"fact": "Travelling in Lisbon 03-12 Sep", "scope": {"customer_id": "CUST00001"}}
         guard: clean (0 rule hits)
         grain: Where the customer is, as a trip window. No year, because a memory that dates itself goes stale; the year is on the travel notice row. This is the fact an agent needs to know a Lisbon charge is not automatically suspicious.
```

…seven more…

```
  [9/9] POST https://.../memories
         body: {"fact": "Notify before closing any dispute", "scope": {"customer_id": "CUST00001"}}
         guard: clean (0 rule hits)
         grain: A durable instruction that outlives this case. Buried in a paragraph it is invisible to a query about contact preferences; on its own it retrieves for 'what has this customer asked us to do'.

--------------------------------------------------------------------------
provenance of this plan: {"LIVE_API": 0, "SEEDED": 10, "COMPUTED": 4, "STATIC": 0, "STATIC_WAIVED": 0, "STATIC_UNWAIVED": 0}
--------------------------------------------------------------------------
```

With no ids in the environment, the same command renders every id as `n/r` — no invented
project, no invented engine.

### 4.3 The seeder refuses a bad fact

I appended a deliberately bad fact to `ATOMIC_FACTS` at runtime and called `main(['--dry-run'])`:

```
Refusing to seed. One of the facts in this script is not allowed in memory:
memory rejected - this fact carries a value that BigQuery owns.
  fact    : 'ATOMIC_FACTS[9]: Customer disputes EUR 412 on DSP00034, provisional credit due 23 Sep 2026'
  hit 1   : 'EUR 412'  [rule: currency_amount, at chars 18:25]
  hit 2   : 'DSP00034'  [rule: dispute_id, at chars 29:37]
  hit 3   : '23 Sep 2026'  [rule: dated_deadline, at chars 62:73]
  fix     : state the situation in words and let the reader fetch the number, date or id from BigQuery at use time.
seeder exit code: 3
```

Nothing was written or even planned — the guard runs before the plan is printed.

### 4.4 The seeder refuses a live run with no ids

```
$ python3 scripts/seed_atomic_memories.py     # with the env vars unset
A live run needs --project and --engine-id (or the matching environment variables).
Refusing to guess: a wrong engine id writes a customer's memories into somebody else's bank.
EXIT=2
```

### 4.5 Reconciler, offline with nothing to read

`python3 scripts/reconcile_scenario.py --dry-run` → exit 0

```
  memory side : not read - offline, and no fixture or --legacy-blob given
  BigQuery    : not queried - offline

  no memory was read, so there is nothing to reconcile and no verdict to give.
  Re-run with --legacy-blob, --memory-fixture, or live credentials.

provenance of this reconciliation: {"LIVE_API": 0, "SEEDED": 1, "COMPUTED": 1, "STATIC": 0, "STATIC_WAIVED": 0, "STATIC_UNWAIVED": 0}

NO VERDICT: the memory side was not read, so this run proves nothing either way. It printed the plan, not a result.
```

"Nothing to check" and "checked, found nothing" print different words on purpose.

### 4.6 Reconciler, pre-fix claims, no authoritative row

`python3 scripts/reconcile_scenario.py --dry-run --legacy-blob` → exit **3**

```
  EUR 412        amount     n/r                        UNVERIFIABLE
      why    : no authoritative row was read, so this claim is unchecked, not clean
  EUR 1480       amount     n/r                        UNVERIFIABLE
  90 days        window     n/r                        UNVERIFIABLE

INCONCLUSIVE: 3 claim(s) could not be checked because no authoritative row was read. This is reported, not passed.
```

An unread BigQuery is `n/r` and a non-zero exit, never a green tick.

### 4.7 Reconciler, pre-fix claims against a captured BigQuery result

With a local fixture holding two dispute rows at USD 1,284.60 and USD 3,940.15
(`--bq-fixture`, not shipped, not read by anything at runtime) → exit **1**

```
  MEMORY CLAIM   KIND       BIGQUERY SAYS              VERDICT
  ---------------------------------------------------------------------------------
  EUR 412        amount     USD 1284.6 / USD 3940.15   MISMATCH
      column : disputed_amount_usd
      why    : memory says EUR 412, BigQuery says USD 1284.6 / USD 3940.15. BigQuery wins.

  EUR 1480       amount     USD 1284.6 / USD 3940.15   MISMATCH
      column : disputed_amount_usd
      why    : memory says EUR 1480, BigQuery says USD 1284.6 / USD 3940.15. BigQuery wins.

  90 days        window     90                         MATCHES-BUT-STILL-WRONG-HOME
      column : investigation_window_days
      why    : the window agrees today, but it is policy plus a filing date, both of which are looked up.

provenance of this reconciliation: {"LIVE_API": 0, "SEEDED": 6, "COMPUTED": 3, "STATIC": 0, "STATIC_WAIVED": 0, "STATIC_UNWAIVED": 0}

FAIL: 2 claim(s) in memory disagree with BigQuery. BigQuery is authoritative; the memory must be re-seeded without them (scripts/seed_atomic_memories.py --purge-blob).
```

### 4.8 Reconciler against the nine post-fix facts — the target state

Same BigQuery fixture, memory side = the nine facts this seeder writes → exit **0**

```
  memory side : memory fixture fixtures/post_fix_memories.json
  BigQuery    : captured BigQuery result fixtures/bq_disputes_capture.json

  no memory in scope asserts an amount, a deadline or a dispute id.
  That is the target state: memory describes, BigQuery quantifies.

PASS: memory asserts no amount, deadline or dispute id.
```

### 4.9 Syntax and import checks

```
$ python3 -c "import ast; ast.parse(open('backend/memory_bank.py').read()); ast.parse(open('backend/knowledge_catalog.py').read()); print('AST OK')"
AST OK
```

All three scripts parse the same way. `py_compile` is broken in this environment, as noted.

---

## 5. What I could NOT verify

Be sceptical of anything in this section — it is written but unrun.

1. **Every live Memory Bank call.** No network, no credentials, no Google SDK. Unrun:
   `create_memory`, `delete_memory`, the `wait_for_operation` loop around create, and the
   live purge path. The request shapes follow the documented v1beta1 REST surface
   (`POST …/memories` with `{fact, scope}`; `DELETE …/memories/{id}`), and both reuse the
   client's existing, already-working `_request` — but nobody has seen a 200 from them.
2. **The `create` response shape.** I read the new memory's resource name from
   `operation.response.name` through the client's `field()` helper, so if the service nests
   it differently the script prints `n/r` rather than crashing or inventing a name. Worth a
   five-minute check on the first live run.
3. **The real BigQuery column names.** `backend/queries/golden/*.sql` does not exist in this
   repo copy, so I could not read the disputes schema. The reconciler tries a candidate list
   per field, prints the column it used, and reports `UNVERIFIABLE` when none matches. The
   candidates came from `backend/models.py` (`disputed_amount_usd`,
   `provisional_credit_due_date`, `dispute_id`); they are a guess and should be confirmed.
4. **The live golden-query path in the reconciler.** Importing `backend.adk_agents` needs
   ADK, pydantic, certifi and dotenv, none of which are installed here. The import is lazy
   so the offline path works; the live branch is unrun.
5. **Whether the disputes really are USD 1,284.60 and 3,940.15.** I took those from the task
   statement. Nothing I ship asserts them: they exist only in a local test fixture I did not
   deliver. The first live `reconcile_scenario.py` run is what confirms them.
6. **That CUST00001's Lisbon trip starts on the 3rd.** The transcript only says the travel
   notice runs "through the twelfth". `03-12 Sep` came from the task's example of correct
   grain. The end date is grounded; the start date is not. If that matters, read it from the
   travel-notice row and drop the fact, or trim it to "Travelling in Lisbon until 12 Sep".
7. **Whether anything still imports the four `_old` names.** I confirmed
   `GroundTruthTelemetryStore` is imported by `app.py`, `agent.py` and
   `veracity_and_audit.py`; those call sites belong to other workers. `CustomerMemoryBank`,
   `KNOWN_CUSTOMERS` and `get_or_create_reasoning_engine_id` were also exported by the
   dead bridge and may have callers I have not traced.

---

## 6. Things wrong outside my files

Ordered by how much they hurt.

1. **`adk_agents.py` turns a failed BigQuery query into a healthy-looking row.**
   `execute_golden_query_contract` (~line 280) ends with:

   ```python
   except Exception as exc:  # pragma: no cover
       rows = [{"note": f"BigQuery execution note: {exc}"}]
   ```

   The result then reports `row_count = 1`. A missing table, a bad credential, an absent SQL
   file — all come back as one row and a success-shaped payload. This is the same failure
   mode as the `except Exception: pass` I just removed, and it affects all twelve golden
   queries. It should re-raise, or return `{"error": ..., "rows": [], "row_count": 0}`.
   Everything I deliver depends on BigQuery being trustworthy, so this one matters most.

2. **`backend/queries/golden/` does not exist in this repo.** `load_golden_sql_text` reads
   `backend/queries/golden/NN_*.sql`, falling back to a `jpmc_fraud_demo_kit/...` path that
   is also absent. Combined with (1), every golden query currently fails and reports one
   fake note row instead. The twelve `.sql` files need to be in the repo.

3. **Hardcoded project id as a fallback, in three places.** `adk_agents.py` line 256,
   `memory_bank.create_default_memory_bank_client`, and
   `knowledge_catalog.create_default_catalog_governor` all do
   `os.environ.get("GOOGLE_CLOUD_PROJECT", "abhinayboda-genai-bb")`, and the memory one also
   defaults the engine id to `"4277754991222980608"`. That is someone's personal project
   baked into a customer kit. I left the two in my files untouched because removing them is
   more than minimal surgery and would change the constructor contract — but they should
   become `require_env`. Note the defaults make a misconfigured run *look* like it works,
   which is exactly what the provenance module exists to prevent.

4. **The compaction surface is still live elsewhere.** `POST /api/memory/compact` and
   `POST /api/compaction/run` in `app.py`, `mb.compact_memories()`, `CompactionReport` and
   `is_compacted_summary` in `models.py`, the `#btnRunCompaction` button and its hardcoded
   "68.8% saved" label in `frontend/static/js/app.js`, and
   `tests/test_backend.py::test_memory_compact_endpoint`, which asserts
   `token_reduction_pct >= 80.0`. Item 2 removes the concept from the memory layer; these
   five call sites keep it alive in the UI and the test suite. They belong to other workers.

5. **The README claims things the code does not do.** It advertises 768-dimensional
   `text-embedding-005` vectors with "exact mathematical cosine similarity" for both Memory
   Bank retrieval and the Knowledge Catalog, and a "Dreaming Service" using
   `DynamicCompactionSchema` with `count_tokens()` for "~70–85% context window compression".
   Neither `memory_bank.py` nor `knowledge_catalog.py` contains any of it — both say in
   their headers that retrieval and ranking happen inside the managed service, which is the
   correct design. The README describes the deleted `_old` modules.

6. **The scenario currency is inconsistent across the repo.** `agent.py`, `app.py`,
   `veracity_and_audit.py` and `index.html` all state **EUR 620** and **EUR 1,480.00** for
   DSP00033 / DSP00034 as literals in prose and in UI copy. The authoritative BigQuery
   amounts are in USD. Whatever the disputes table says, those hardcoded strings will
   contradict it, and there are enough of them that a reader will see the seam. The same
   files also hardcode the derived Reg E date (`23 Sep 2026`) and the 90-day window rather
   than deriving them from the filing date.

7. **`veracity_and_audit.py` line 404 keyword-matches on `"1480"` and `"620"`** to decide
   whether a claim is corroborated. That hard-wires the audit sweep to the pre-fix numbers:
   once BigQuery is the source, the sweep will fail to corroborate the correct amounts.

---

## 7. Files delivered

| File | Status |
|---|---|
| `backend/memory_bank.py` | bridge removed; `create_memory` / `delete_memory` added; rest untouched |
| `backend/knowledge_catalog.py` | bridge removed; nothing else changed |
| `scripts/memory_facts.py` | new — the guard. **Run, passes.** |
| `scripts/seed_atomic_memories.py` | new — nine atomic writes. **Dry-run and refusal paths run; live path unrun.** |
| `scripts/reconcile_scenario.py` | new — BigQuery reconciliation. **Offline paths run; live golden-query path unrun.** |
| `reports/memory.md` | this report |
