-- =============================================================================
-- 15_spend_summary.sql
--
-- INTENT
--   How much did this customer spend over a date range, on what, and where?
--   One row: the totals, how many attempts were declined, spend by merchant
--   category and the top merchants. This is "how much did I spend last week?"
--   and "what did I spend most on?" in a customer chat.
--
-- INPUTS
--   @customer_id         STRING  e.g. 'CUST00001'
--   @start_date          DATE    inclusive, UTC calendar day,
--                                e.g. DATE '2026-09-16'
--   @end_date            DATE    inclusive, UTC calendar day,
--                                e.g. DATE '2026-09-22'
--   @top_merchant_limit  INT64   how many merchants to list, e.g. 5
--
-- TABLES USED
--   abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations
--   abhinayboda-genai-bb.jpmc_ccb_fraud.merchants
--
-- WHY THIS IS THE APPROVED WAY
--   Money is summed in amount_usd, never in amount. amount is in the currency
--   of the transaction, and a customer who travelled paid in euros for part of
--   the window; adding euros to dollars gives a total that is simply wrong.
--   amount_usd is the converted value, so the totals add up.
--   Only APPROVED authorizations count as spend. A declined attempt never
--   moved any money, so it is reported separately as declined_count and
--   declined_amount_usd. Adding declines in would inflate spend exactly when a
--   card is frozen and declines pile up, which is when the customer asks.
--   Refunds are approved rows with a negative amount_usd. They net off the
--   total, and refund_count says how many there were so the assistant can
--   explain a total that looks low.
--   The category lives on merchants (merchant_category), not on
--   authorizations, so the query joins merchants. It is a LEFT JOIN: the
--   dataset has authorizations whose merchant reference is orphaned, and an
--   inner join makes that spend vanish. Those rows are bucketed as
--   UNMAPPED_MERCHANT / UNKNOWN_MERCHANT so the total still adds up and the
--   gap is visible.
--   The window is filtered on auth_timestamp, the time the card was used, and
--   not on settled_date or created_at, which are posting and load times. The
--   UTC calendar day is used so the boundaries match query 12.
--   is_fraud is not used. It is the demo's answer key, and a customer's spend
--   includes every approved charge until a dispute says otherwise.
--   Everything comes back in one row with the breakdowns as arrays, so the
--   totals and the breakdowns cannot be separated by a row limit, and an
--   empty window still returns a row of zeros rather than nothing. Those
--   zeros are measured: the window was read and nothing was in it.
--
-- EXPECTED SHAPE
--   Exactly 1 row. spend_by_category is an array of (merchant_category,
--   approved_count, approved_amount_usd), biggest spend first. top_merchants
--   is an array of (merchant_id, merchant_name, merchant_category,
--   approved_count, approved_amount_usd), biggest spend first, at most
--   @top_merchant_limit entries.
-- =============================================================================

WITH window_auths AS (
  SELECT
    a.decision,
    a.amount_usd,
    a.merchant_id,
    COALESCE(m.merchant_name, 'UNKNOWN_MERCHANT') AS merchant_name,
    COALESCE(m.merchant_category, 'UNMAPPED_MERCHANT') AS merchant_category
  FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations` AS a
  LEFT JOIN `abhinayboda-genai-bb.jpmc_ccb_fraud.merchants` AS m
    ON a.merchant_id = m.merchant_id
  WHERE a.customer_id = @customer_id
    AND DATE(a.auth_timestamp, 'UTC') BETWEEN @start_date AND @end_date
),
totals AS (
  SELECT
    COUNTIF(decision = 'APPROVED') AS approved_count,
    COALESCE(SUM(IF(decision = 'APPROVED', amount_usd, NULL)), NUMERIC '0') AS approved_amount_usd,
    COUNTIF(decision = 'APPROVED' AND amount_usd < 0) AS refund_count,
    COUNTIF(decision = 'DECLINED') AS declined_count,
    COALESCE(SUM(IF(decision = 'DECLINED', amount_usd, NULL)), NUMERIC '0') AS declined_amount_usd
  FROM window_auths
),
by_category AS (
  SELECT
    merchant_category,
    COUNT(*) AS approved_count,
    SUM(amount_usd) AS approved_amount_usd
  FROM window_auths
  WHERE decision = 'APPROVED'
  GROUP BY merchant_category
),
by_merchant AS (
  SELECT
    merchant_id,
    merchant_name,
    merchant_category,
    COUNT(*) AS approved_count,
    SUM(amount_usd) AS approved_amount_usd
  FROM window_auths
  WHERE decision = 'APPROVED'
  GROUP BY merchant_id, merchant_name, merchant_category
)
SELECT
  @customer_id AS customer_id,
  @start_date AS window_start_date,
  @end_date AS window_end_date,
  t.approved_count,
  t.approved_amount_usd,
  t.refund_count,
  t.declined_count,
  t.declined_amount_usd,
  ARRAY(
    SELECT AS STRUCT merchant_category, approved_count, approved_amount_usd
    FROM by_category
    ORDER BY approved_amount_usd DESC, merchant_category ASC
  ) AS spend_by_category,
  ARRAY(
    SELECT AS STRUCT merchant_id, merchant_name, merchant_category, approved_count, approved_amount_usd
    FROM by_merchant
    ORDER BY approved_amount_usd DESC, merchant_id ASC
    LIMIT @top_merchant_limit
  ) AS top_merchants
FROM totals AS t;
