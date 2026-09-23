-- =============================================================================
-- 12_fraud_rate_by_merchant_category.sql
--
-- INTENT
--   Which kinds of merchant are we losing money to? Grouped by merchant
--   category over a date range, how many authorizations, how many turned out
--   to be fraud, and what share of the money that was. This is the portfolio
--   view a fraud strategy lead uses to decide where to tighten rules.
--
-- INPUTS
--   @start_date       DATE    inclusive, e.g. DATE '2026-06-23'
--   @end_date         DATE    inclusive, e.g. DATE '2026-09-21'
--   @min_auth_count   INT64   drop categories thinner than this, e.g. 25
--
-- TABLES USED
--   {dataset}.authorizations
--   {dataset}.merchants
--
-- WHY THIS IS THE APPROVED WAY
--   This is the one query in the set that is allowed to use is_fraud, and the
--   reason is worth stating because it is the opposite of the rule in query
--   03. is_fraud is confirmed history, written after the fact. Looking
--   backwards at what turned out to be fraud is exactly what a portfolio
--   report is for. Using it to decide whether a transaction happening right
--   now is fraud would be reading the answer key, which is why query 03 does
--   not touch it.
--   Three things a hand-written version gets wrong.
--   It inner joins merchants, so authorizations whose merchant reference is
--   orphaned disappear. Those rows carry real money, and dropping them makes
--   every category's fraud rate look slightly better than it is. We LEFT JOIN
--   and bucket the orphans into a visible UNMAPPED_MERCHANT category, so the
--   total still adds up and the data gap is on screen instead of hidden.
--   It divides with a plain slash and the query fails or returns nothing
--   useful the moment a category has zero rows in the range. SAFE_DIVIDE
--   returns NULL instead of erroring, which is the honest answer for "no
--   denominator".
--   It reports a fraud rate on a category with four transactions and someone
--   quotes "50% fraud in PET SUPPLIES" in a meeting. @min_auth_count exists to
--   stop that, and the threshold is a parameter so the person running it has
--   to choose it rather than inherit someone's guess.
--   Counting is by transaction and by amount, both, because they tell
--   different stories: card testing is thousands of tiny attempts and barely
--   any money, while one fraudulent watch purchase is one transaction and
--   thousands of dollars.
--
-- EXPECTED SHAPE
--   Roughly 8 to 15 rows, one per merchant category that cleared
--   @min_auth_count, highest fraud rate first. fraud_rate and
--   fraud_amount_share are both 0.0 to 1.0.
-- =============================================================================

SELECT
  COALESCE(m.merchant_category, 'UNMAPPED_MERCHANT') AS merchant_category,
  COUNT(*) AS auth_count,
  COUNTIF(a.decision = 'APPROVED') AS approved_count,
  COUNTIF(a.decision = 'DECLINED') AS declined_count,
  COUNT(DISTINCT a.card_id) AS distinct_cards,
  COUNT(DISTINCT a.merchant_id) AS distinct_merchants,
  COUNTIF(a.is_fraud) AS fraud_count,
  ROUND(SAFE_DIVIDE(COUNTIF(a.is_fraud), COUNT(*)), 4) AS fraud_rate,
  SUM(a.amount_usd) AS total_amount_usd,
  SUM(IF(a.is_fraud, a.amount_usd, NUMERIC '0')) AS fraud_amount_usd,
  ROUND(
    SAFE_DIVIDE(
      SUM(IF(a.is_fraud, a.amount_usd, NUMERIC '0')),
      SUM(a.amount_usd)
    ),
    4
  ) AS fraud_amount_share,
  ROUND(SAFE_DIVIDE(SUM(a.amount_usd), COUNT(*)), 2) AS avg_amount_usd,
  COUNTIF(a.amount_usd < 0) AS negative_amount_rows,
  COUNTIF(m.merchant_id IS NULL) AS rows_with_unmapped_merchant,
  MIN(DATE(a.auth_timestamp, 'UTC')) AS first_activity_date,
  MAX(DATE(a.auth_timestamp, 'UTC')) AS last_activity_date
FROM `{dataset}.authorizations` AS a
LEFT JOIN `{dataset}.merchants` AS m
  ON a.merchant_id = m.merchant_id
WHERE DATE(a.auth_timestamp, 'UTC') BETWEEN @start_date AND @end_date
GROUP BY merchant_category
HAVING auth_count >= @min_auth_count
ORDER BY fraud_rate DESC, auth_count DESC;
