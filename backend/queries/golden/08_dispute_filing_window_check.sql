-- =============================================================================
-- 08_dispute_filing_window_check.sql
--
-- INTENT
--   Was this dispute filed in time, and once it was filed, what does the bank
--   now owe and by when? Two separate questions that people mix up constantly.
--   The first is about the customer's deadline to report. The second is about
--   the bank's deadlines to respond, which are different numbers under a
--   different rule depending on whether the account is a deposit account or a
--   credit card.
--
-- INPUTS
--   @dispute_id   STRING   e.g. 'DSP00012'
--
-- TABLES USED
--   {dataset}.disputes
--   {dataset}.authorizations
--   {dataset}.accounts
--
-- WHY THIS IS THE APPROVED WAY
--
--   1. It separates the customer's clock from the bank's clock.
--   filing_window_days is the customer-facing reporting window - how long the
--   customer had to tell us. Under both rules that is 60 days
--   (12 CFR 1005.11(b)(1) for Reg E, 12 CFR 1026.13(b)(1) for Reg Z). Passing
--   that check does not tell you anything about what happens next. The bank's
--   own deadlines start when the notice arrives and are a completely separate
--   set of numbers, so this query returns both and labels them.
--   Honest caveat about this dataset: the regulation counts the 60 days from
--   the periodic statement on which the error first appeared, while this
--   dataset counts days_since_transaction from the transaction date, because
--   the synthetic data has no statement cycle. It is a deliberate
--   simplification, it is stated here rather than hidden, and it makes the
--   window slightly stricter than the real rule, never looser.
--
--   2. It refuses to take the stored answer on trust. The disputes table
--   already carries is_within_filing_window, and an agent that just reads it
--   will be right most of the time and catastrophically wrong when the column
--   is stale or was written by a broken job. So the query recomputes from
--   first principles and returns the stored value beside the recomputed value
--   plus a flag saying whether they agree. Disagreement is a finding to
--   escalate, not to paper over.
--
--   3. It gets the two regulations right, which is where most agents fall
--   over. Regulation E covers electronic transfers on deposit accounts, so
--   debit cards on checking and savings. Regulation Z covers credit cards.
--   They are not interchangeable:
--     - REG_E: the bank investigates within 10 business days, and if it cannot
--       finish, it must issue provisional credit within those 10 business days
--       and then has up to 45 calendar days total (12 CFR 1005.11(c)(1)-(2)).
--     - REG_E extended: 20 business days for the credit and up to 90 calendar
--       days for the investigation, where the transaction was initiated
--       outside the United States, was a point-of-sale debit card transaction,
--       or happened within 30 days of the first deposit to a new account
--       (12 CFR 1005.11(c)(3)). This query works out which of those three
--       triggers fired and names them.
--     - REG_Z: no provisional credit at all. The creditor acknowledges in
--       writing within 30 calendar days and must resolve within two complete
--       billing cycles and in no event later than 90 calendar days
--       (12 CFR 1026.13(c)). Meanwhile it may not collect the disputed amount,
--       may not charge finance charges on it, and may not report it delinquent
--       (12 CFR 1026.13(d)).
--   An agent that promises a credit card customer a provisional credit is
--   giving a wrong answer with regulatory consequences, so this query states
--   provisional_credit_applies as an explicit boolean rather than letting the
--   agent infer it.
--
--   4. Two date details that are easy to get wrong. The window is counted from
--   the date of the transaction, not the date it settled and not the statement
--   date, so we take DATE(auth_timestamp, 'UTC') rather than settled_date. And
--   the timezone is pinned to UTC explicitly, because a transaction just
--   before midnight UTC shifts a whole day if the session timezone decides,
--   and one day is exactly the margin that settles a borderline case.
--
--   5. The regulation path is checked in two places - on the dispute and on
--   the account it belongs to - because the dispute inherits it from the
--   account and a mismatch means the wrong rulebook was applied to the whole
--   case. The query reports both rather than quietly picking one.
--
--   Filing window lengths are not hard coded here. They live in
--   filing_window_days on the dispute row, populated from the single
--   definition in catalog/glossary.yaml, so the number exists in one place.
--
-- EXPECTED SHAPE
--   Exactly 0 or 1 rows. 0 means the dispute id does not exist. The row
--   carries the filing verdict in is_within_filing_window_recomputed, the
--   stored value for comparison, the deadline date itself,
--   stored_matches_recomputed which must be TRUE in a healthy dataset, and
--   then the bank-side picture: applicable_rule, provisional_credit_applies,
--   on_extended_reg_e_clock, extended_clock_triggers, and a plain-English
--   downstream_obligation_summary the agent can read out.
-- =============================================================================

WITH base AS (
  SELECT
    d.dispute_id,
    d.customer_id,
    d.account_id,
    d.card_id,
    d.auth_id,
    d.opened_date,
    d.opened_channel,
    d.status,
    d.disputed_amount,
    d.provisional_credit_issued,
    d.provisional_credit_due_date,
    d.investigation_due_date,
    d.regulation_path AS dispute_regulation_path,
    acct.regulation_path AS account_regulation_path,
    acct.account_type,
    acct.open_date AS account_open_date,
    d.filing_window_days,
    d.days_since_transaction AS days_since_transaction_stored,
    d.is_within_filing_window AS is_within_filing_window_stored,
    a.auth_timestamp,
    a.amount_usd AS authorized_amount_usd,
    a.country_code AS auth_country_code,
    a.channel AS auth_channel,
    DATE(a.auth_timestamp, 'UTC') AS transaction_date,
    DATE_DIFF(d.opened_date, DATE(a.auth_timestamp, 'UTC'), DAY) AS days_since_transaction_recomputed,
    DATE_ADD(DATE(a.auth_timestamp, 'UTC'), INTERVAL d.filing_window_days DAY) AS filing_deadline_date
  FROM `{dataset}.disputes` AS d
  LEFT JOIN `{dataset}.authorizations` AS a
    ON d.auth_id = a.auth_id
  LEFT JOIN `{dataset}.accounts` AS acct
    ON d.account_id = acct.account_id
  WHERE d.dispute_id = @dispute_id
),
regime AS (
  SELECT
    b.dispute_id,
    b.customer_id,
    b.account_id,
    b.card_id,
    b.auth_id,
    b.opened_date,
    b.opened_channel,
    b.status,
    b.disputed_amount,
    b.provisional_credit_issued,
    b.provisional_credit_due_date,
    b.investigation_due_date,
    b.dispute_regulation_path,
    b.account_regulation_path,
    b.account_type,
    b.account_open_date,
    b.filing_window_days,
    b.days_since_transaction_stored,
    b.is_within_filing_window_stored,
    b.auth_timestamp,
    b.authorized_amount_usd,
    b.auth_country_code,
    b.auth_channel,
    b.transaction_date,
    b.days_since_transaction_recomputed,
    b.filing_deadline_date,
    ARRAY(
      SELECT trigger_name
      FROM UNNEST([
        IF(b.auth_country_code != 'US', 'TRANSACTION_INITIATED_OUTSIDE_THE_US', NULL),
        IF(b.auth_channel = 'CARD_PRESENT', 'POINT_OF_SALE_DEBIT_TRANSACTION', NULL),
        IF(
          DATE_DIFF(b.transaction_date, b.account_open_date, DAY) <= 30,
          'NEW_ACCOUNT_WITHIN_30_DAYS_OF_FIRST_DEPOSIT',
          NULL
        )
      ]) AS trigger_name
      WHERE trigger_name IS NOT NULL
    ) AS reg_e_extension_triggers
  FROM base AS b
)
SELECT
  r.dispute_id,
  r.customer_id,
  r.account_id,
  r.card_id,
  r.auth_id,
  r.account_type,
  r.dispute_regulation_path,
  r.account_regulation_path,
  (r.dispute_regulation_path = r.account_regulation_path) AS regulation_path_matches_account,

  -- The customer's clock: did they report it in time?
  r.transaction_date,
  r.auth_timestamp,
  r.auth_country_code,
  r.auth_channel,
  r.authorized_amount_usd,
  r.disputed_amount,
  r.opened_date,
  r.opened_channel,
  r.status,
  r.filing_window_days,
  r.filing_deadline_date,
  r.days_since_transaction_stored,
  r.days_since_transaction_recomputed,
  (r.days_since_transaction_stored = r.days_since_transaction_recomputed) AS days_since_transaction_matches,
  r.is_within_filing_window_stored,
  (r.days_since_transaction_recomputed <= r.filing_window_days) AS is_within_filing_window_recomputed,
  (r.is_within_filing_window_stored = (r.days_since_transaction_recomputed <= r.filing_window_days))
    AS stored_matches_recomputed,
  DATE_DIFF(r.filing_deadline_date, r.opened_date, DAY) AS days_of_margin_at_filing,
  CASE
    WHEN r.auth_timestamp IS NULL
      THEN 'CANNOT_DECIDE_DISPUTED_AUTHORIZATION_NOT_FOUND'
    WHEN r.days_since_transaction_recomputed <= r.filing_window_days
      THEN 'WITHIN_FILING_WINDOW'
    ELSE 'OUTSIDE_FILING_WINDOW'
  END AS verdict,

  -- The bank's clock: what do we now owe, and under which rulebook?
  CASE
    WHEN r.dispute_regulation_path = 'REG_E' THEN 'REG_E_12_CFR_1005_11'
    WHEN r.dispute_regulation_path = 'REG_Z' THEN 'REG_Z_12_CFR_1026_13'
    ELSE 'UNKNOWN_REGULATION_PATH'
  END AS applicable_rule,
  (r.dispute_regulation_path = 'REG_E') AS provisional_credit_applies,
  (r.dispute_regulation_path = 'REG_E' AND ARRAY_LENGTH(r.reg_e_extension_triggers) > 0)
    AS on_extended_reg_e_clock,
  IF(r.dispute_regulation_path = 'REG_E', r.reg_e_extension_triggers, [])
    AS extended_clock_triggers,
  r.provisional_credit_issued,
  r.provisional_credit_due_date,
  r.investigation_due_date,
  CASE
    WHEN r.dispute_regulation_path = 'REG_E' AND ARRAY_LENGTH(r.reg_e_extension_triggers) > 0
      THEN 'Regulation E, extended time limits. Provisional credit is due within 20 business days of the notice and the investigation may run up to 90 calendar days, because the transaction was initiated outside the US, was a point-of-sale debit transaction, or hit a new account within 30 days of its first deposit.'
    WHEN r.dispute_regulation_path = 'REG_E'
      THEN 'Regulation E, standard time limits. Provisional credit is due within 10 business days of the notice if the investigation is not finished, and the investigation may run up to 45 calendar days.'
    WHEN r.dispute_regulation_path = 'REG_Z'
      THEN 'Regulation Z. There is no provisional credit. The bank acknowledges in writing within 30 calendar days and must resolve within two complete billing cycles and no later than 90 calendar days. Meanwhile it may not collect the disputed amount, charge finance charges on it, or report it delinquent.'
    ELSE 'Regulation path is missing or unrecognised, so no deadline can be stated. Escalate rather than guess.'
  END AS downstream_obligation_summary
FROM regime AS r;
