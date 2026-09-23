-- =============================================================================
-- 05_merchant_velocity_spike.sql
--
-- INTENT
--   Which merchants had a day that looks nothing like their normal day? A
--   merchant that usually sees 20 cards a day and suddenly sees 400, or whose
--   fraud rate jumps from near zero to a third of its volume, is either
--   breached or is the drop point someone is pushing stolen cards through.
--   A fraud analyst asks this to find the common merchant behind several
--   unrelated-looking customer reports.
--
-- INPUTS
--   @merchant_id      STRING NULLABLE   pass NULL to scan every merchant, or
--                                       one id to drill into a single one
--   @start_date       DATE              inclusive
--   @end_date         DATE              inclusive
--   @min_fraud_rate   FLOAT64           e.g. 0.05
--   @min_auth_count   INT64             ignore days thinner than this, e.g. 10
--   @velocity_flags   ARRAY<STRING>     e.g. ['ELEVATED','SPIKE']; pass an
--                                       empty array to accept every flag
--
-- TABLES USED
--   abhinayboda-genai-bb.jpmc_ccb_fraud.agg_merchant_velocity_daily
--
-- WHY THIS IS THE APPROVED WAY
--   A naive agent recomputes all of this from the 18,000-row authorizations
--   table every time it is asked. That is slower and, worse, it drifts: two
--   agents computing "fraud rate" slightly differently will give a customer
--   two different numbers for the same merchant. The daily aggregate is the
--   agreed definition, computed once, and it reconciles exactly to
--   authorizations, so everyone quotes the same figure.
--   The second mistake is reading a raw daily number as a spike. 40
--   authorizations is a quiet Tuesday for a supermarket and a five-alarm fire
--   for a jeweller. So this query compares each day against that same
--   merchant's own trailing 30-day average, which is the only comparison that
--   means anything, and returns the ratio next to the raw number.
--   The third is trusting fraud_rate blindly. This dataset deliberately
--   contains at least one row where fraud_rate sits outside 0 to 1, which is
--   impossible for a rate and means the source row is broken. Rather than let
--   that row quietly dominate a "worst merchants" list, the query labels it in
--   fraud_rate_quality and the agent is expected to report it as a data
--   problem, not as a fraud finding.
--   The trailing average uses a RANGE frame over the date converted to an
--   integer day number, not ROWS, because a merchant with no activity on a
--   Sunday has no row for that Sunday, and a ROWS frame would silently reach
--   further back than 30 days.
--
-- EXPECTED SHAPE
--   0 to a few hundred rows, one per merchant-day that cleared the filters,
--   worst fraud rate first. Days flagged CHECK_FAILED_OUT_OF_RANGE in
--   fraud_rate_quality are data-quality findings, not fraud findings.
-- =============================================================================

WITH scoped AS (
  SELECT
    v.activity_date,
    v.merchant_id,
    v.merchant_name,
    v.mcc,
    v.country_code,
    v.auth_count,
    v.approved_count,
    v.declined_count,
    v.distinct_customers,
    v.distinct_cards,
    v.total_amount_usd,
    v.avg_amount_usd,
    v.max_amount_usd,
    v.fraud_count,
    v.fraud_rate,
    v.velocity_flag,
    v.computed_at
  FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.agg_merchant_velocity_daily` AS v
  WHERE (@merchant_id IS NULL OR v.merchant_id = @merchant_id)
),
with_baseline AS (
  SELECT
    s.activity_date,
    s.merchant_id,
    s.merchant_name,
    s.mcc,
    s.country_code,
    s.auth_count,
    s.approved_count,
    s.declined_count,
    s.distinct_customers,
    s.distinct_cards,
    s.total_amount_usd,
    s.avg_amount_usd,
    s.max_amount_usd,
    s.fraud_count,
    s.fraud_rate,
    s.velocity_flag,
    s.computed_at,
    AVG(s.auth_count) OVER trailing_30d AS baseline_auth_count_30d,
    AVG(s.distinct_cards) OVER trailing_30d AS baseline_distinct_cards_30d,
    AVG(s.fraud_rate) OVER trailing_30d AS baseline_fraud_rate_30d
  FROM scoped AS s
  WINDOW trailing_30d AS (
    PARTITION BY s.merchant_id
    ORDER BY UNIX_DATE(s.activity_date)
    RANGE BETWEEN 30 PRECEDING AND 1 PRECEDING
  )
)
SELECT
  b.activity_date,
  b.merchant_id,
  b.merchant_name,
  b.mcc,
  b.country_code,
  b.auth_count,
  b.approved_count,
  b.declined_count,
  b.distinct_customers,
  b.distinct_cards,
  b.total_amount_usd,
  b.avg_amount_usd,
  b.max_amount_usd,
  b.fraud_count,
  b.fraud_rate,
  b.velocity_flag,
  ROUND(b.baseline_auth_count_30d, 2) AS baseline_auth_count_30d,
  ROUND(b.baseline_distinct_cards_30d, 2) AS baseline_distinct_cards_30d,
  ROUND(b.baseline_fraud_rate_30d, 4) AS baseline_fraud_rate_30d,
  ROUND(SAFE_DIVIDE(b.auth_count, b.baseline_auth_count_30d), 2) AS auth_count_vs_baseline_ratio,
  ROUND(SAFE_DIVIDE(b.distinct_cards, b.baseline_distinct_cards_30d), 2) AS distinct_cards_vs_baseline_ratio,
  CASE
    WHEN b.fraud_rate IS NULL THEN 'CHECK_FAILED_NULL'
    WHEN b.fraud_rate < 0 OR b.fraud_rate > 1 THEN 'CHECK_FAILED_OUT_OF_RANGE'
    ELSE 'OK'
  END AS fraud_rate_quality,
  b.computed_at
FROM with_baseline AS b
WHERE b.activity_date BETWEEN @start_date AND @end_date
  AND b.auth_count >= @min_auth_count
  AND (ARRAY_LENGTH(@velocity_flags) = 0 OR b.velocity_flag IN UNNEST(@velocity_flags))
  AND (
    b.fraud_rate IS NULL
    OR b.fraud_rate >= @min_fraud_rate
    OR b.fraud_rate > 1
    OR b.fraud_rate < 0
  )
ORDER BY fraud_rate_quality DESC, b.fraud_rate DESC, b.auth_count DESC;
