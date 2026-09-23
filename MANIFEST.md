# MANIFEST — applying the consumer-credit rework

Repo: `~/Downloads/jpmc-consumer-credit`

Read this first: nothing in this set has been run against Vertex AI, Dataplex or
BigQuery. It parses, the route set is unchanged, and the offline harnesses pass.
Whether it works live is unproven. See REWORK_REPORT.md, "Verified vs unrun".

Two things will stop the app starting even after a clean apply. Both are named
in "Before you start the server" below. Read that section before you run
anything.

---

## The short version

```bash
export REPO=~/Downloads/jpmc-consumer-credit
export DROP=<directory holding the delivered tree>

cd $REPO
git status                      # commit or stash local work first
git checkout -b rework-apply

cp $DROP/backend/*.py            $REPO/backend/
cp $DROP/frontend/static/js/app.js $REPO/frontend/static/js/
mkdir -p $REPO/scripts $REPO/reports
cp $DROP/scripts/*.py            $REPO/scripts/
cp $DROP/reports/*.md            $REPO/reports/

python3 -c "import ast,glob; [ast.parse(open(f).read(),f) for f in glob.glob('backend/*.py')+glob.glob('scripts/*.py')]; print('AST OK')"
python3 scripts/memory_facts.py
python3 scripts/reconcile_scenario.py --dry-run
```

Expect `AST OK`, then exit 0 from both scripts. If all three pass you have
applied the set correctly. You have not yet shown that the app runs.

The delivered tree keeps the repo's own layout (`backend/`, `frontend/`,
`scripts/`, `reports/`), so the copies above are path-for-path.

---

## The 16 files

### Backend

| File | Replaces or new | Why |
|---|---|---|
| `backend/provenance.py` | new | The tagging harness every other file imports. Provides `seeded` / `live` / `computed` / `unmeasured` / `identity` / `static`, and makes an unmeasured value render as the literal `n/r`. |
| `backend/memory_bank.py` | replaces | Removes the `memory_bank_old` compatibility shim that was swallowing an ImportError. Adds `create_memory` and `delete_memory` so single facts can be written and removed. |
| `backend/knowledge_catalog.py` | replaces | Removes the matching `knowledge_catalog_old` shim. Nine deleted lines and one heading; nothing else changed. |
| `backend/adk_agents.py` | replaces | Restores the 8-agent roster and real hub-and-spoke topology. Adds the five channel producers, which read seeded Memory Bank sessions. Makes a failed BigQuery query raise instead of returning a fake row. |
| `backend/veracity_and_audit.py` | replaces | Claim validator now runs on BigQuery golden queries plus one Gemini call, not keyword matching. Fires the Dataplex deprecated-table and restricted-column traps. |
| `backend/app.py` | replaces | Drops the phantom imports, removes the hand-written "findings", puts provenance on the scorecard, and stops reporting unmeasured metrics as perfect scores. |
| `backend/agent.py` | replaces | Rebuilt against the real field names in `models.py`. The old version constructed `CausalStep` and `SynthesisResult` with fields that do not exist. |

### Frontend

| File | Replaces or new | Why |
|---|---|---|
| `frontend/static/js/app.js` | replaces | Fixes six broken endpoint calls, subscribes to the SSE stream nobody was listening to, and renders provenance chips. Removes the hardcoded 68.8% saving that displayed even when the request failed. |

### Scripts (all new)

| File | Why |
|---|---|
| `scripts/verify_provenance.py` | Checks a payload for untagged or unwaived-static values. Run it in CI. |
| `scripts/memory_facts.py` | The write-time guard. Rejects any memory carrying an amount, a date, a duration or a record id. |
| `scripts/seed_atomic_memories.py` | Writes the nine atomic facts for CUST00001, one `memories.create` call each. `--purge-blob` removes the old narrative. |
| `scripts/reconcile_scenario.py` | Arbitrates memory claims against BigQuery. BigQuery wins; a claim it cannot check is reported, not passed. |

### Reports (all new, reference only)

`reports/channels.md`, `reports/memory.md`, `reports/validator.md`,
`reports/wiring.md` — the four engineers' own accounts, including what each
could not verify.

---

## Safe to drop in, versus check first

Safe to drop in. These are new files with no prior version in the repo:

- `backend/provenance.py`
- all four `scripts/*.py`
- all four `reports/*.md`

Check for local edits before overwriting. These are whole-file replacements, so
anything you changed locally is gone:

| File | How much of it changed |
|---|---|
| `backend/memory_bank.py` | Small. Bridge block deleted, 37 lines added. A local edit elsewhere in the file is probably easy to re-apply. |
| `backend/knowledge_catalog.py` | Smallest in the set. Nine lines deleted. |
| `backend/adk_agents.py` | Large. Roster rewritten, 14 functions added, none removed. |
| `backend/veracity_and_audit.py` | Near-total rewrite. ~650 lines of keyword logic and prompt scaffolding deleted. Public method names and signatures are unchanged. |
| `backend/app.py` | Large. ~250 lines of fabricated findings deleted, scorecard rebuilt. Route set unchanged. |
| `backend/agent.py` | Rewritten against `models.py`. |
| `frontend/static/js/app.js` | Large. Six handlers rewritten, one new subscriber function, a provenance rendering layer added. |

Quickest way to see what you would lose:

```bash
cd $REPO
for f in backend/app.py backend/agent.py backend/adk_agents.py \
         backend/veracity_and_audit.py backend/memory_bank.py \
         backend/knowledge_catalog.py frontend/static/js/app.js; do
  diff -q "$f" "$DROP/$f" >/dev/null || echo "differs: $f"
done
```

Not touched by this set, and still holding stale content:
`backend/models.py`, `frontend/templates/index.html`, `tests/test_backend.py`,
`agents.json`, `README.md`.

---

## Before you start the server

One real prerequisite. Two earlier "blockers" were checked against the actual
repo and are not blockers at all — see the note below so you do not chase them.

### Required: set the engine id

There is no longer a hardcoded fallback for the reasoning engine id. This is
deliberate: a literal default meant a misconfigured run silently talked to one
specific project's engine and still looked healthy. The client now raises when
the id is unset, which is the outcome you want.

```bash
export GOOGLE_CLOUD_PROJECT=<your project>
export VERTEX_AGENT_ENGINE_ID=<your reasoning engine id>
export GOOGLE_CLOUD_LOCATION=us-central1
```

If you skip this, `backend/app.py` raises on import with a message naming the
variable. That is the intended behaviour, not a bug.

### Not blockers — verified against the repo

- `backend/custom_agent_comparison.py` **exists** (21 KB, `AgentComparisonEngine`
  at line 303) and the new `app.py` imports it at line 63. An earlier draft of
  this manifest called it missing; that was written without the file in view.
- `backend/queries/golden/` **exists** in the repo, and the demo kit carries all
  twelve `.sql` files. The loader reads `backend/queries/golden/`, which is
  present. Also not a blocker.

---

## What to run after applying, in order

Steps 1 to 5 need no credentials. Steps 6 onward do.

### 1. Everything parses

```bash
python3 -c "import ast,glob; [ast.parse(open(f).read(),f) for f in glob.glob('backend/*.py')+glob.glob('scripts/*.py')]; print('AST OK')"
```

Good output: `AST OK`. Use `ast.parse`, not `py_compile` — `py_compile` was
broken in the build environment and may be here too.

### 2. The dead symbols are really gone

```bash
grep -rn "memory_bank_old\|knowledge_catalog_old\|GroundTruthTelemetryStore\|CustomerMemoryBank\|AWSOnPremHybridConnector" backend/ frontend/
```

Good output: nothing. Any hit in `backend/` is a file that did not get replaced.
Hits in `README.md` are expected and are a documentation problem, not a code one.

### 3. No fabrication patterns survive

```bash
grep -rn "api_called\|text-embedding\|cosine\|fallback triggered\|68.8" backend/ frontend/static/js/
```

Good output: nothing.

### 4. The memory guard

```bash
python3 scripts/memory_facts.py
```

Good output: exit 0, ending `result: 0 failure(s)`. It rejects 12 strings the
old blob asserted and accepts the 9 replacement facts. A rejection prints the
matched text, the rule, the character offsets and the reason.

### 5. The reconciler, offline

```bash
python3 scripts/reconcile_scenario.py --dry-run
python3 scripts/verify_provenance.py
```

Good output for the reconciler: exit 0, ending `NO VERDICT: the memory side was
not read, so this run proves nothing either way.` That is correct. It is
reporting that it had nothing to check, not that the check passed.

`verify_provenance.py` has no report behind it, so I cannot tell you what good
output looks like. Treat a non-zero exit as a failure and read what it prints.

### 6. First real import

```bash
python3 -c "import backend.app; print('imports clean')"
```

This is the first step that exercises flask, pydantic and the Google SDK. It has
never been run by anyone. If it fails on `custom_agent_comparison`, see
"Before you start the server".

### 7. Start it, check the roster

```bash
python3 backend/app.py
curl -s localhost:${PORT:-8080}/api/agents/list | python3 -m json.tool | head -30
```

Good output: `count` is 8, with names `fraud_monitoring_agent`,
`telephony_ivr_agent`, `mobile_app_agent`, `web_portal_agent`,
`branch_support_agent`, `claim_veracity_validator_agent`,
`memory_bank_audit_agent`, `consumer_credit_synthesizer_agent`.

If it returns 6, `adk_agents.py` did not get replaced.

### 8. The first live check that matters

```bash
python3 -m backend.memory_bank sessions
```

Compare the `sessionState.channel` labels in the output against
`CHANNEL_SESSION_TOKENS` in `adk_agents.py`. This is the single biggest runtime
risk in the set. The five channel producers filter sessions by channel label,
and the seeded corpus does not spell channels consistently — the repo's own
seeder writes `IVR` where `models.py` says `TELEPHONY_IVR`. The alias table
covers the spellings that were known. If the 52 seeded sessions use a spelling
it does not cover, producers return empty rather than wrong. You will see it as
`session_count: 0`, not as a bad row.

### 9. Fix the memory shape

```bash
export GOOGLE_CLOUD_PROJECT=... VERTEX_AGENT_ENGINE_ID=... GOOGLE_CLOUD_LOCATION=us-central1
python3 scripts/seed_atomic_memories.py --dry-run --purge-blob   # read the plan
python3 scripts/seed_atomic_memories.py --purge-blob --yes       # do it
```

The dry run prints the scope, the engine URL, the nine facts and the delete
template, and touches nothing. Unsupplied ids render as `n/r` and stop a live
run — they are never defaulted to a plausible-looking literal.

The live purge refuses to run without `--yes`. It prints the resource name, the
reason and the first 200 characters of every memory it is about to delete,
before deleting it.

Good output: nine `create` calls, each waiting on its operation, then a retrieve
showing nine memories in scope.

### 10. Confirm memory and BigQuery agree

```bash
python3 scripts/reconcile_scenario.py
```

Good output: exit 0, ending `PASS: memory asserts no amount, deadline or dispute
id.`

Exit 1 means a stored memory contradicts BigQuery — re-run step 9 with
`--purge-blob`. Exit 3 means it could not read an authoritative row, so it
checked nothing; that is a BigQuery access problem, not a memory problem.

---

## Rollback

If you branched, as in the short version:

```bash
cd $REPO
git checkout -- backend/ frontend/static/js/app.js
git checkout main && git branch -D rework-apply
```

If the repo is not under git, take a backup before you copy anything:

```bash
cd $REPO
mkdir -p .backup-preapply
cp -R backend frontend/static/js .backup-preapply/
```

and restore with:

```bash
cp -R .backup-preapply/backend/. backend/
cp .backup-preapply/js/app.js frontend/static/js/app.js
```

The new files — `backend/provenance.py`, the four scripts, the four reports —
have no prior version, so rollback leaves them in place. They are inert unless
something imports or runs them. `provenance.py` is the exception: the restored
backend files do not import it, so leaving it costs nothing.

There is no data to roll back until you run step 9. After a live
`--purge-blob`, the deleted memories are gone from Memory Bank and this set
contains nothing that restores them. The old blob can be re-created from
`LISBON_THEFT_TURNS` in `backend/memory_bank.py` if you ever need it back.
