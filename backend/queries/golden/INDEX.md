# Golden query index

These are the twelve approved queries for the JPMC CCB fraud demo dataset. They
all run against BigQuery project `abhinayboda-genai-bb`, dataset
`jpmc_ccb_fraud`, and they all use `@named` parameters so nothing is pasted
into a string.

"Golden" means the catalog points at these as the right way to ask the
question. An agent is expected to pick one of these rather than write its own
SQL, because each one already handles a mistake that is easy to make and hard
to spot. Every file says what that mistake is, in its own header, under WHY
THIS IS THE APPROVED WAY.

Two rules hold across all twelve: no query ever selects `ssn_last4` from
`customers` or `pan_full` from `cards`, and no query reads
`authorizations_deprecated_v1`. Those are the two governance traps, explained
in `quality/governance_traps.md`.

## The twelve

| # | File | Question it answers, in plain English | Agent that calls it | Key parameters |
|---|---|---|---|---|
| 01 | `01_customer_360_snapshot.sql` | Who is this customer and what do their last 90 days look like? | Identity & Context | `@customer_id` |
| 02 | `02_recent_authorizations_for_card.sql` | What are the most recent charges on this card? | Fraud Triage | `@card_id`, `@as_of_ts`, `@lookback_days`, `@row_limit` |
| 03 | `03_suspicious_activity_on_card_window.sql` | Between these two moments, what on this card is worth a second look, and why? | Fraud Triage | `@card_id`, `@window_start_ts`, `@window_end_ts`, `@min_risk_score` |
| 04 | `04_geo_impossible_travel.sql` | Was this card used in two places too far apart to travel between in the time available? | Fraud Triage | `@card_id`, `@as_of_ts`, `@lookback_days`, `@max_plausible_kmh`, `@min_distance_km` |
| 05 | `05_merchant_velocity_spike.sql` | Which merchants had a day that looks nothing like their own normal day? | Fraud Triage | `@merchant_id`, `@start_date`, `@end_date`, `@min_fraud_rate`, `@min_auth_count`, `@velocity_flags` |
| 06 | `06_cnp_burst_detection.sql` | Did someone fire a rapid run of online charges at this card? | Fraud Triage | `@card_id`, `@window_start_ts`, `@window_end_ts`, `@burst_window_seconds`, `@min_burst_count` |
| 07 | `07_open_disputes_for_customer.sql` | What disputes does this customer already have running? | Policy & Compliance, read by the Orchestrator before it opens anything new | `@customer_id`, `@as_of_date`, `@open_statuses` |
| 08 | `08_dispute_filing_window_check.sql` | Was this dispute filed in time, and what does the bank now owe and by when? | Policy & Compliance | `@dispute_id` |
| 09 | `09_provisional_credit_due.sql` | Which Regulation E disputes owe the customer a provisional credit, and when is it due? | Policy & Compliance | `@as_of_date`, `@horizon_days`, `@warn_days`, `@open_statuses` |
| 10 | `10_wallet_tokens_active_on_card.sql` | Which phones and watches can still spend on this card? | Resolution / Actions | `@card_id`, `@as_of_ts`, `@usage_lookback_days`, `@include_inactive` |
| 11 | `11_dispute_event_timeline.sql` | Everything that happened on this case, in order. | Resolution / Actions, and the Memory Writer when it summarises the session | `@dispute_id`, `@as_of_date`, `@pre_open_hours`, `@post_close_hours` |
| 12 | `12_fraud_rate_by_merchant_category.sql` | Which kinds of merchant are we losing money to? | Fraud Triage, on the portfolio question rather than a single case | `@start_date`, `@end_date`, `@min_auth_count` |

## Which agent is which

The demo runs six agents. Four of them read these queries.

- **Orchestrator** — talks to the customer, routes work, owns the
  conversation. Calls 01 and 07 at the start of a session so it knows who it is
  talking to and what is already open.
- **Identity & Context** — reads Vertex AI Memory Bank for what this person has
  told us before, and calls 01 for the hard facts that live in BigQuery rather
  than in memory.
- **Fraud Triage** — scores the transactions. Calls 02, 03, 04, 05, 06 and 12.
  This is the agent most likely to invent its own SQL if the golden set does
  not cover the question, which is why the fraud side has six of the twelve.
- **Policy & Compliance** — resolves definitions and deadlines. Calls 07, 08
  and 09. This is the agent that has to be able to say no, so 08 recomputes
  rather than trusting a stored flag.
- **Resolution / Actions** — freezes cards, revokes tokens, issues virtual
  cards, reroutes couriers. Calls 10 before it claims a card is locked down,
  and 11 to read back what it has already done.
- **Memory Writer** — after the session, writes the extracted facts back to
  Memory Bank. Calls 11 to get the ordered event list it summarises from.

## Two notes on how these are meant to be read

**Ground truth is off limits during triage.** `authorizations.is_fraud` is the
label we generated so the demo can be scored. Query 03 deliberately does not
use it, because an agent reading the label is reading the answer key and the
triage would look perfect for a reason that would not exist in production.
Query 12 is the one exception and says so in its header: a backwards-looking
portfolio report is exactly what a confirmed-fraud label is for.

**Regulation E and Regulation Z are not interchangeable.** Regulation E covers
electronic transfers on deposit accounts, so debit cards on checking and
savings. Regulation Z covers credit cards. Provisional credit — the bank
putting the disputed money back while it investigates — is a Regulation E duty
only. Regulation Z has no provisional credit at all; instead the bank may not
collect the disputed amount, charge finance charges on it, or report it
delinquent while it investigates. Query 09 therefore filters to Regulation E
cases, and query 08 reports which rulebook applies, whether provisional credit
is owed at all, and whether the case is on Regulation E's extended clock
because the transaction was initiated outside the United States, was a
point-of-sale debit transaction, or hit an account less than 30 days old.
An agent that promises a credit card customer a provisional credit has given a
wrong answer with regulatory consequences, and that is the specific failure
these two queries exist to prevent.
