-- =============================================================================
-- 09_provisional_credit_due.sql
--
-- INTENT
--   Which disputes owe the customer a provisional credit, and when is it due?
--   Provisional credit means the bank puts the disputed money back in the
--   account while it investigates. There is a hard deadline for doing it, and
--   missing it is a regulatory breach, not an inconvenience. A fraud
--   operations lead runs this every morning.
--
-- INPUTS
--   @as_of_date      DATE            the date the agent is reasoning from,
--                                    e.g. DATE '2026-09-21'
--   @horizon_days    INT64           look this far ahead, e.g. 5
--   @warn_days       INT64           how many days ahead counts as DUE_SOON,
--                                    e.g. 2
--   @open_statuses   ARRAY<STRING>   statuses still eligible for a credit,
--                                    e.g. ['OPEN','UNDER_REVIEW']
--
-- TABLES USED
--   {dataset}.disputes
--   {dataset}.accounts
--   {dataset}.authorizations
--
-- WHY THIS IS THE APPROVED WAY
--   The first and biggest thing this query gets right is WHO is owed a
--   provisional credit at all. Provisional credit is a Regulation E duty, and
--   Regulation E covers electronic transfers on deposit accounts - checking
--   and savings, so debit cards. Regulation Z covers credit cards, and
--   Regulation Z has no provisional credit duty whatsoever. Under Reg Z the
--   bank instead may not collect the disputed amount, may not charge finance
--   charges on it, and may not report it delinquent while it investigates
--   (12 CFR 1026.13(d)). Those are different obligations with different
--   deadlines, and they are not a credit into the account.
--   So this query filters to regulation_path = 'REG_E' and says so in the
--   output. An agent that leaves credit card disputes in this list will chase
--   operations for a payment that is not owed, and worse, may tell a credit
--   card customer to expect money that is never coming. That is a wrong answer
--   a compliance reviewer will spot immediately. Reg Z cases still need
--   watching, but on their own clock - see query 08, which reports the Reg Z
--   acknowledgement and resolution deadlines.
--   The second rule is what this query does NOT do: it does not recompute the
--   due date. Under Reg E the credit is due within 10 business days of the
--   notice, or 20 business days when the extended-time rules apply
--   (12 CFR 1005.11(c)), and business days skip weekends and US federal
--   holidays. An agent that recomputes that with DATE_ADD and calendar days
--   will be two days out on most cases and three on a case opened the Friday
--   before a Monday holiday. provisional_credit_due_date is calculated once,
--   by the generator, using the business-day calendar, and every reader takes
--   it as given. That is the whole reason the column exists.
--   Third, it filters on provisional_credit_issued rather than on status
--   alone. A dispute at PROVISIONAL_CREDIT_ISSUED has already been paid and
--   does not need chasing, but a dispute still at UNDER_REVIEW with the flag
--   false and a due date in the past is a live breach. Status alone cannot
--   tell those apart.
--   The credit amount defaults to the disputed amount where no explicit credit
--   amount is set, because that is what the bank owes if nobody has decided
--   otherwise, and an agent reporting NULL there reads as "nothing owed",
--   which is the wrong answer.
--   The query also reports on_extended_reg_e_clock so the operations lead can
--   see which cases are on the longer 20-business-day and 90-calendar-day
--   track. It is context, not arithmetic - the dates themselves still come
--   from the stored columns.
--
-- EXPECTED SHAPE
--   0 to about 20 rows, most overdue first, Regulation E cases only. One row
--   per dispute with an outstanding credit obligation falling on or before
--   @as_of_date + @horizon_days. due_status is one of OVERDUE, DUE_TODAY,
--   DUE_SOON, SCHEDULED. regulation_path is always 'REG_E' by construction.
-- =============================================================================

WITH obligations AS (
  SELECT
    d.dispute_id,
    d.customer_id,
    d.account_id,
    d.card_id,
    d.auth_id,
    acct.account_type,
    acct.open_date AS account_open_date,
    d.regulation_path,
    d.opened_date,
    d.opened_channel,
    d.status,
    d.assigned_queue,
    d.dispute_reason_code,
    d.dispute_reason,
    d.disputed_amount,
    d.provisional_credit_issued,
    d.provisional_credit_date,
    COALESCE(d.provisional_credit_amount, d.disputed_amount) AS credit_amount_owed,
    d.provisional_credit_due_date,
    d.investigation_due_date,
    a.auth_timestamp,
    a.country_code AS auth_country_code,
    a.channel AS auth_channel,
    DATE_DIFF(d.provisional_credit_due_date, @as_of_date, DAY) AS days_until_due,
    DATE_DIFF(@as_of_date, d.opened_date, DAY) AS days_open
  FROM `{dataset}.disputes` AS d
  LEFT JOIN `{dataset}.accounts` AS acct
    ON d.account_id = acct.account_id
  LEFT JOIN `{dataset}.authorizations` AS a
    ON d.auth_id = a.auth_id
  WHERE d.regulation_path = 'REG_E'
    AND d.status IN UNNEST(@open_statuses)
    AND d.provisional_credit_issued = FALSE
    AND d.resolved_date IS NULL
)
SELECT
  o.dispute_id,
  o.customer_id,
  o.account_id,
  o.card_id,
  o.auth_id,
  o.account_type,
  o.regulation_path,
  o.status,
  o.assigned_queue,
  o.dispute_reason_code,
  o.dispute_reason,
  o.disputed_amount,
  o.credit_amount_owed,
  o.opened_date,
  o.opened_channel,
  o.auth_timestamp,
  o.days_open,
  o.provisional_credit_due_date,
  o.investigation_due_date,
  o.days_until_due,
  CASE
    WHEN o.days_until_due < 0 THEN 'OVERDUE'
    WHEN o.days_until_due = 0 THEN 'DUE_TODAY'
    WHEN o.days_until_due <= @warn_days THEN 'DUE_SOON'
    ELSE 'SCHEDULED'
  END AS due_status,
  (
    o.auth_country_code != 'US'
    OR o.auth_channel = 'CARD_PRESENT'
    OR DATE_DIFF(DATE(o.auth_timestamp, 'UTC'), o.account_open_date, DAY) <= 30
  ) AS on_extended_reg_e_clock,
  ARRAY(
    SELECT trigger_name
    FROM UNNEST([
      IF(o.auth_country_code != 'US', 'TRANSACTION_INITIATED_OUTSIDE_THE_US', NULL),
      IF(o.auth_channel = 'CARD_PRESENT', 'POINT_OF_SALE_DEBIT_TRANSACTION', NULL),
      IF(
        DATE_DIFF(DATE(o.auth_timestamp, 'UTC'), o.account_open_date, DAY) <= 30,
        'NEW_ACCOUNT_WITHIN_30_DAYS_OF_FIRST_DEPOSIT',
        NULL
      )
    ]) AS trigger_name
    WHERE trigger_name IS NOT NULL
  ) AS extended_clock_triggers
FROM obligations AS o
WHERE o.days_until_due <= @horizon_days
ORDER BY o.days_until_due ASC, o.credit_amount_owed DESC, o.dispute_id ASC;
