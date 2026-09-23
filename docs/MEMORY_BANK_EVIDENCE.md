# Memory Bank: what it actually does

This document records what Vertex AI Agent Engine Memory Bank was **observed**
to do against a live engine in this project. Every claim below is reproducible
with the harness in `backend/memory_eval/`. Where a number is not measured it is
written `n/r` rather than guessed.

Nothing here is drawn from the product documentation. The point of the exercise
was to stop describing the service and start measuring it.

Reproduce everything:

```bash
set -a && . ./.env && set +a
PYTHONPATH=. ./.venv/bin/python3 -m backend.memory_eval.runner --json-out results.json
```

Individual stages: `--only retrieval|inclusion|topics|scope|consolidation|reconcile`.

---

## 1. The question this answers

> How does the memory bank decide what to write and what to exclude?
> Is retrieval precise, and is it complete?

Short answer, established experimentally below:

| Mechanism | What it controls | Evidence |
|---|---|---|
| **Topics** | *Whether a fact is extracted at all.* An inclusion filter applied during extraction, not a label attached afterwards. | §4 |
| **Scope** | *Where a fact lands and who may read it.* A hard partition, matched exactly. | §5 |
| **Consolidation** | *Whether a new fact becomes a second record or revises the first.* | §6 |
| **Similarity search** | *What comes back for a question.* Measured for precision and recall in §3. | §3 |

---

## 2. The API, as observed

Base: `https://{location}-aiplatform.googleapis.com/v1beta1/{engine}` where
`{engine}` is `projects/{project_number}/locations/{location}/reasoningEngines/{engine_id}`.

> [!NOTE]
> The service echoes the **project number** back in `memory.name`, not the
> project id. `Settings.agent_engine_resource` prefers the number for exactly
> that reason, so resource names compare equal.

| Call | Body | Returns |
|---|---|---|
| `POST {engine}/memories:retrieve` | `{"scope":{...},"similaritySearchParams":{"searchQuery":"...","topK":n}}` | `{"retrievedMemories":[{"memory":{...},"distance":0.857}]}` |
| `POST {engine}/memories:retrieve` | `{"scope":{...},"simpleRetrievalParams":{"pageSize":n}}` | scope-wide list, **no `distance` field** |
| `GET {engine}/memories?filter=...` | — | `{"memories":[...],"nextPageToken":...}` |
| `POST {engine}/memories:generate` | `{"scope":{...},"directContentsSource":{"events":[...]}}` | a long-running operation |
| `POST {engine}/memories` | verbatim record | creates without extraction |
| `DELETE {engine}/memories/{id}` | — | — |

Three details that cost time to discover, recorded so they do not have to be
discovered again:

**`distance`, not similarity.** Smaller is closer. A UI that sorts descending
and calls it "relevance" will show the worst matches first.

**A `done: true` response with no `response` key means nothing was written.**
This is not an error and it is not an empty list — the key is simply absent.
It is the single most informative outcome the API produces, and
`GenerateResult.wrote_nothing` is built on it.

**The LRO response does not contain the facts.** Items in
`response.generatedMemories[]` look like
`{"memory":{"name":...,"memoryType":...},"action":"CREATED"|"UPDATED"|"DELETED"}`.
There is no `fact` field. Any harness that reports what was written by reading
the LRO is reporting nothing. `run_inclusion` therefore reads the corpus back
with `list_memories` afterwards.

**Topic filtering is not supported server-side.** `filter='topics.label="X"'`
returns `400 INVALID_ARGUMENT: No field filter plugin found for path
[topics.label]`. Four spellings were tried; all were rejected. This repository
does not ship a client-side post-filter dressed up as a service capability.

### Two different topic shapes

A real source of bugs, so both are handled explicitly:

| Where | Shape |
|---|---|
| Engine configuration | `{"customMemoryTopic":{"label":"TRAVEL_STATUS","description":"..."}}` |
| On a stored memory | `{"customMemoryTopicLabel":"TRAVEL_STATUS"}` |

Parsed by `MemoryTopic.from_api()` and `_topic_labels()` respectively in
[`backend/memory_bank.py`](../backend/memory_bank.py).

---

## 3. Retrieval: precision and recall

`backend/memory_eval/retrieval_eval.py`, driven by twelve probes in
[`benchmarks/retrieval_probes.json`](../benchmarks/retrieval_probes.json).

### How the ground truth is established

The probes contain **no memory ids**. Hard-coding "probe X should return memory
m7" would make the benchmark a test of whether the corpus had changed, not of
whether retrieval works. Instead each probe carries a question, `anchor_terms`,
`expected_topics` and a `ground_truth_anchor`, and relevance is judged against
the corpus at run time by two independent labellers:

- **`AnchorRelevanceLabeller`** — deterministic word-boundary matching on the
  anchor terms. It *under-labels* by design: it cannot recognise a paraphrase,
  so recall computed against it is a pessimistic bound.
- **`LlmRelevanceLabeller`** — Gemini 2.5 Flash at temperature 0, structured
  output, one call per probe over the whole corpus.

Reporting the agreement between the two is the honest move: if they diverge
sharply, the number to trust is neither one alone. `agreement()` reports Jaccard
overlap on the positive sets, and `self_consistency` runs the judge twice to
show how stable it is.

### Metric conventions

`precision`, `recall` and `f1` return **`None` when undefined, never `0.0`**. A
probe for which nothing is relevant has undefined precision; recording that as
zero would drag a mean down with a value that was never measured. `aggregate()`
excludes `None` and reports `n_undefined` alongside the mean, so a number
computed from eight of twelve probes cannot be mistaken for one computed from
twelve.

`k = 5` was fixed in advance, before any results were seen, and the reason is
stated in `RetrievalReport.headline()`. The full sweep over k ∈ {1, 3, 5, 10} is
reported too, but the headline number is not chosen after the fact.

### One probe is designed to fail

`london_distractor`. The corpus contains a memory asserting travel to **London**.
BigQuery holds exactly one travel notice for this customer and it is **Lisbon**.
A retrieval system that returns the London memory for "where did the customer
travel" is behaving correctly — the memory really is the most similar text. The
failure is downstream, and §7 is where it is caught. This probe exists to
demonstrate that high retrieval precision and a wrong answer are compatible,
which is the entire argument for reconciling memory against the warehouse.

### Results

See `--only retrieval --json-out`. The report contains, per probe: precision,
recall and F1 at each k, **every false positive named**, **every miss named**,
MRR, nDCG and average precision, plus the labeller agreement. A probe that
scores badly is listed with the specific memories it wrongly returned, not
summarised into an average.

---

## 4. Topics decide what is written

**This is the decisive experiment.** `backend/memory_eval/write_policy.py`,
`run_topic_comparison`.

The difficulty: you cannot change the topic vocabulary of a live engine without
destroying the corpus you are measuring. So `temporary_engine()` creates a
throwaway reasoning engine per arm, runs the identical transcript against it,
and deletes it in a `finally`. Engine creation completes in roughly one second.

Three arms. Same transcript, same scope, same API call. **Only the topic
vocabulary differs**, so it is the only thing that can explain a difference in
outcome.

| Arm | Topics |
|---|---|
| `desk_custom` | the seven custom fraud-desk topics |
| `managed_defaults` | the four managed topics, stated explicitly |
| `custom_plus_managed` | all eleven |

> [!IMPORTANT]
> "No topics configured" is **not** a neutral control. If `customizationConfigs`
> is absent or `memoryTopics` is empty, the server substitutes all four managed
> defaults. A harness that treats the empty config as "topics off" is measuring
> the defaults and calling it a baseline. `TopicConfig.managed_defaults()`
> therefore states the four explicitly.

The four managed topics — the only valid values — are `USER_PERSONAL_INFO`,
`USER_PREFERENCES`, `KEY_CONVERSATION_DETAILS`, `EXPLICIT_INSTRUCTIONS`.
Custom and managed topics may be mixed in one configuration.

### Result

| Transcript | `desk_custom` | `managed_defaults` | `custom_plus_managed` |
|---|---|---|---|
| `mixed_signal_call` | 4 | 4 | 4 |
| `personal_info_only` | **0** | **2** | **2** |

The first row is the control: a transcript whose content every vocabulary covers
produces the same four memories regardless. The second row is the finding. A
transcript of ordinary personal chatter produced **zero** memories under the
fraud-desk vocabulary and **two** under the managed defaults.

The transcript, the scope and the API call were identical across arms. The topic
vocabulary is therefore the cause.

**Topics are an inclusion filter applied during extraction, not a label applied
to whatever was extracted anyway.** Narrowing the vocabulary does not merely
change how memories are tagged; it changes whether they exist. This is the
control surface for what a memory bank is permitted to remember.

### The same thing by hand

Two `generateMemories` calls against the live engine, seven custom topics:

- *"My name is Dana and I always fly out of Newark. Also, what is the weather
  today?"* → operation `done: true`, **no `response` field**. Scope retrieval
  returns `{}`. **Nothing written.** None of it was fraud-desk relevant.
- *"I will be in Lisbon from October 2 to October 11, so please do not block my
  card there. Also what is the weather like today? And my favourite colour is
  blue."* → **one memory CREATED**, topic `TRAVEL_STATUS`, fact: *"I will be
  traveling to Lisbon from October 2 to October 11, and requested that my card
  not be blocked during this period."*

The weather question and the favourite colour were discarded from a transcript
that also contained a durable fact. Extraction is selective within a single
turn, not merely across turns.

---

## 5. Write inclusion: what is kept and what is dropped

`run_inclusion` over `mixed_signal_call`, nine utterances, each labelled in
[`benchmarks/write_transcripts.json`](../benchmarks/write_transcripts.json) as:

- **`durable`** — should be remembered
- **`disposable`** — small talk, should not
- **`warehouse`** — a figure that BigQuery owns authoritatively and the memory
  bank should not duplicate, because a copied number goes stale silently

### Result

| Measure | Value |
|---|---|
| Durable utterances retained | **4 of 4** (`durable_coverage = 1.0`) |
| Write precision | **1.0** |
| Disposable content leaked | **none** |
| Warehouse figures leaked | **none** |

The four memories written, with the topics the service assigned:

| Topic(s) | Fact |
|---|---|
| `TRAVEL_STATUS` | Porto, 14–29 November; card not to be frozen |
| `AGENT_COMMITMENTS`, `DISPUTE_HISTORY` | agent promised a callback before Friday |
| `DEVICE_WALLET_INVENTORY` | watch lost, token deactivation requested |
| `CONTACT_PREFERENCE` | text alerts to the mobile ending 0198 instead of email |

The result worth dwelling on is the third row of the table above. The transcript
states a statement balance of "4,280 dollars and 50 cents". **It was not
stored.** That figure lives in BigQuery, where it changes; a memory asserting it
would have been correct on the day it was written and wrong thereafter, with
nothing to signal the difference.

### Attribution, and a bug worth recording

Deciding which utterance produced which memory is a matching problem, and the
naive version is wrong. Plain token overlap linked utterance `u06` to the Porto
memory because both contained the word **"time"**. That single shared common
word was enough to report a leak that had not occurred.

`attribute()` is now IDF-weighted with `threshold=0.22` and `min_shared=2`, so a
link requires more than one token and rare tokens count for more than common
ones. After the fix `disposable_leaked_ids` is `[]`, where it had been `["u06"]`.

This is recorded because the harness's own measurement error produced a false
positive against the service. A benchmark that is not itself debugged will
happily report the service's failings and its own in the same column.

---

## 6. Scope is a hard partition

`run_scope_isolation` and `run_consolidation`.

> A semantically near-identical query against a different scope returned **0 of
> 4**. Scope is a hard partition, not a ranking signal — retrieval cannot leak
> across it, which is what makes it safe as a tenant boundary. Adding one extra
> key to the scope returned **0**, so scope matching is exact, not subset. A
> per-session scope therefore cannot read customer-level memories: the scope you
> write with is the scope you must read with.

That last sentence is the operational consequence. Scoping writes per session
and then expecting a customer-level read to find them yields silence, not a
partial result — and silence is indistinguishable from an empty history.

### Consolidation

> Restating the same fact more precisely produced an **UPDATE**: the corpus
> stayed at 1. The service recognised one underlying fact and revised it rather
> than storing a near-duplicate. This is the behaviour that separates a memory
> bank from an append-only log. After an explicit contradiction the corpus held
> **0** memories, with actions `['DELETED']`.

A memory bank that appended on every restatement would accumulate several
versions of one fact with no way to tell which is current, and retrieval would
return all of them.

---

## 7. Memory against ground truth

`backend/memory_eval/reconcile.py`. Memory Bank records what a customer *said*.
BigQuery records what *happened*. They can disagree, and the disagreement is the
interesting signal — so this corpus was deliberately left as it was rather than
being re-seeded.

Two live contradictions:

1. A `TRAVEL_STATUS` memory asserts **London**. The warehouse holds one travel
   notice: **Lisbon**, 2026-08-28 to 2026-09-18, returned early on 2026-09-10.
2. Memories describe disputed amounts in prose — "412 euros", "90 euros" —
   against warehouse figures of 796.42, 715.61, 1065.76 and others.

Two implementation notes:

**Currency words, not codes.** `CURRENCY_WORDS` matches spelled-out names
(`euros?`, `dollars?`) because Memory Bank writes prose. A matcher expecting
`EUR` finds nothing and reports a clean corpus.

**Not every number is a claim.** `AUTHORITATIVE_PATTERNS` (amounts) can produce a
contradiction verdict. `HYGIENE_PATTERNS` (`card_suffix`, `bare_quantity`)
**never** do, because a card suffix is four digits and would otherwise be
compared against dispute amounts as though it were money.

---

## 8. What is not established

Stated plainly, because a document that only lists successes is not evidence.

- **Absolute retrieval quality is bounded by the labellers.** The anchor
  labeller under-labels; the LLM judge is a model. Agreement between them is
  reported; neither is ground truth.
- **Twelve probes over one customer's corpus** is a diagnostic, not a
  statistically meaningful benchmark. It is sized to catch a broken retriever,
  not to certify one.
- **The topic experiment used two transcripts.** The effect on
  `personal_info_only` is unambiguous and the mechanism follows from it, but the
  magnitude of the effect on arbitrary traffic is not characterised.
- **`disable_natural_language_memories`** was not tested.
- **`customizationConfig.scopeKeys`** is not exercised by the harness.
- **Latency figures are per-call wall-clock** from `time.perf_counter()`, taken
  on a single machine against one region. They are `measured()`, but they are
  not a benchmark of the service.
- **Consolidation was tested with explicit restatement and explicit
  contradiction.** Subtle partial contradiction was not.
