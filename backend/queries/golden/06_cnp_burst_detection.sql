-- =============================================================================
-- 06_cnp_burst_detection.sql
--
-- INTENT
--   Did someone fire a rapid run of online, card-not-present charges at this
--   card in a short space of time? "Card not present" means the physical card
--   was never used, only the number, so this is the shape you see when a card
--   number has leaked and is being spent or tested online before the real
--   owner notices.
--
-- INPUTS
--   @card_id                 STRING     e.g. 'CARD00013'
--   @window_start_ts         TIMESTAMP  inclusive lower bound of the search
--   @window_end_ts           TIMESTAMP  inclusive upper bound of the search
--   @burst_window_seconds    INT64      the sliding window width, in seconds,
--                                       e.g. 3600 for one hour
--   @min_burst_count         INT64      how many card-not-present attempts in
--                                       that window count as a burst, e.g. 5
--
-- TABLES USED
--   abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations
--   abhinayboda-genai-bb.jpmc_ccb_fraud.merchants
--
-- WHY THIS IS THE APPROVED WAY
--   The common wrong answer counts transactions per calendar hour with a GROUP
--   BY on the truncated timestamp. That splits a real burst across the hour
--   boundary: six attempts from 10:58 to 11:03 become three and three, and
--   nothing fires. A sliding window that looks back @burst_window_seconds from
--   each transaction has no boundary to fall across, so it catches the burst
--   wherever it sits on the clock.
--   The window width is a parameter in seconds rather than minutes because
--   BigQuery wants a plain constant in a RANGE frame, and multiplying a
--   parameter inside the frame clause is not accepted. Passing seconds
--   directly keeps the frame legal and keeps the width configurable.
--   Two more things a hand-written version misses. It filters to
--   decision = 'APPROVED' and so loses card testing entirely, because card
--   testing is mostly declines by design - the attacker is checking which
--   numbers are live. And it ignores how many different merchants the burst
--   touched, which is the thing that separates a customer buying five items
--   from one shop from an attacker spraying a number across five shops. This
--   query keeps declines and counts distinct merchants inside the window.
--   The distinct count is done by collecting merchant ids into an array over
--   the window and counting distinct values from that array, because BigQuery
--   does not allow COUNT(DISTINCT ...) as a window function.
--
-- EXPECTED SHAPE
--   0 to a few dozen rows. One row per card-not-present authorization whose
--   trailing window is at or over @min_burst_count, newest first. Consecutive
--   rows from the same burst all appear, which is intentional - the agent can
--   read the run and quote its start and end.
-- =============================================================================

WITH cnp AS (
  SELECT
    a.auth_id,
    a.card_id,
    a.customer_id,
    a.merchant_id,
    a.auth_timestamp,
    a.amount_usd,
    a.currency,
    a.entry_mode,
    a.decision,
    a.decline_reason,
    a.risk_score,
    a.risk_band,
    a.country_code,
    a.device_id
  FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations` AS a
  WHERE a.card_id = @card_id
    AND a.channel = 'CARD_NOT_PRESENT'
    AND a.auth_timestamp >= @window_start_ts
    AND a.auth_timestamp <= @window_end_ts
),
rolling AS (
  SELECT
    c.auth_id,
    c.card_id,
    c.customer_id,
    c.merchant_id,
    c.auth_timestamp,
    c.amount_usd,
    c.currency,
    c.entry_mode,
    c.decision,
    c.decline_reason,
    c.risk_score,
    c.risk_band,
    c.country_code,
    c.device_id,
    COUNT(*) OVER burst AS attempts_in_window,
    COUNTIF(c.decision = 'DECLINED') OVER burst AS declines_in_window,
    COUNTIF(c.decision = 'APPROVED') OVER burst AS approvals_in_window,
    SUM(c.amount_usd) OVER burst AS amount_usd_in_window,
    MIN(c.auth_timestamp) OVER burst AS window_first_auth_timestamp,
    ARRAY_AGG(c.merchant_id) OVER burst AS merchant_ids_in_window
  FROM cnp AS c
  WINDOW burst AS (
    PARTITION BY c.card_id
    ORDER BY UNIX_SECONDS(c.auth_timestamp)
    RANGE BETWEEN @burst_window_seconds PRECEDING AND CURRENT ROW
  )
)
SELECT
  r.auth_id,
  r.card_id,
  r.customer_id,
  r.auth_timestamp,
  r.window_first_auth_timestamp,
  TIMESTAMP_DIFF(r.auth_timestamp, r.window_first_auth_timestamp, SECOND) AS burst_span_seconds,
  r.attempts_in_window,
  r.approvals_in_window,
  r.declines_in_window,
  ROUND(SAFE_DIVIDE(r.declines_in_window, r.attempts_in_window), 3) AS decline_ratio_in_window,
  (SELECT COUNT(DISTINCT mid) FROM UNNEST(r.merchant_ids_in_window) AS mid) AS distinct_merchants_in_window,
  r.amount_usd_in_window,
  r.amount_usd,
  r.currency,
  r.entry_mode,
  r.decision,
  r.decline_reason,
  r.risk_score,
  r.risk_band,
  r.country_code,
  r.device_id,
  r.merchant_id,
  COALESCE(m.merchant_name, 'UNKNOWN_MERCHANT') AS merchant_name,
  m.merchant_category
FROM rolling AS r
LEFT JOIN `abhinayboda-genai-bb.jpmc_ccb_fraud.merchants` AS m
  ON r.merchant_id = m.merchant_id
WHERE r.attempts_in_window >= @min_burst_count
ORDER BY r.auth_timestamp DESC;
