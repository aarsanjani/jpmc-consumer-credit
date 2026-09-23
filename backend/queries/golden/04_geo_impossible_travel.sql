-- =============================================================================
-- 04_geo_impossible_travel.sql
--
-- INTENT
--   Did this card get used in two places too far apart for one person to have
--   travelled between them in the time available? If a card is tapped in
--   Lisbon and then swiped in Dallas forty minutes later, one of the two is
--   not the cardholder. This is the single most convincing fraud signal to
--   show a customer, because it needs no model to explain.
--
-- INPUTS
--   @card_id             STRING     e.g. 'CARD00013'
--   @as_of_ts            TIMESTAMP  the "now" the agent reasons from
--   @lookback_days       INT64      e.g. 30
--   @max_plausible_kmh   FLOAT64    fastest believable travel speed including
--                                   airport time, e.g. 900.0
--   @min_distance_km     FLOAT64    ignore pairs closer than this, e.g. 100.0
--
-- TABLES USED
--   abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations
--   abhinayboda-genai-bb.jpmc_ccb_fraud.merchants
--
-- WHY THIS IS THE APPROVED WAY
--   The naive version compares country codes: two different countries within
--   an hour, flag it. That is wrong in both directions. It misses Lisbon to
--   Madrid to Lisbon inside one country pair that is genuinely impossible on
--   the clock, and it screams at a customer who legitimately crossed from
--   Belgium into the Netherlands over lunch. Distance over elapsed time is the
--   only measure that holds up, so we compute real great-circle distance with
--   ST_DISTANCE on geography points and divide by the actual gap.
--   Four more traps this handles. Authorizations with a null latitude or
--   longitude cannot be measured at all, so they are excluded up front rather
--   than treated as coordinate zero, which sits in the Atlantic and generates
--   false alarms. Declined authorizations are excluded because a decline means
--   the card was never actually accepted there. LAG is partitioned by card and
--   ordered by auth_timestamp in UTC, not local_timestamp, because comparing
--   two local clocks in different zones produces a negative or inflated gap.
--   And pairs with a zero-second gap are kept, not silently dropped by a
--   division, because two authorizations in the same second in two cities are
--   the most impossible case there is.
--   Distances come out in metres from ST_DISTANCE, so we divide by 1000 once,
--   here, rather than leaving every caller to remember the unit.
--
-- EXPECTED SHAPE
--   0 to a handful of rows, most impossible first. One row per consecutive
--   pair of authorizations that breaks the speed limit, showing both ends of
--   the pair so a human can read the story without a second query.
--   implied_speed_kmh is NULL when the gap is zero seconds; those sort first.
-- =============================================================================

WITH geo_auths AS (
  SELECT
    a.auth_id,
    a.card_id,
    a.customer_id,
    a.auth_timestamp,
    a.city,
    a.country_code,
    a.latitude,
    a.longitude,
    a.amount_usd,
    a.channel,
    a.entry_mode,
    a.merchant_id
  FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations` AS a
  WHERE a.card_id = @card_id
    AND a.decision = 'APPROVED'
    AND a.latitude IS NOT NULL
    AND a.longitude IS NOT NULL
    AND a.auth_timestamp > TIMESTAMP_SUB(@as_of_ts, INTERVAL @lookback_days DAY)
    AND a.auth_timestamp <= @as_of_ts
),
paired AS (
  SELECT
    g.auth_id AS curr_auth_id,
    g.auth_timestamp AS curr_auth_timestamp,
    g.city AS curr_city,
    g.country_code AS curr_country_code,
    g.latitude AS curr_latitude,
    g.longitude AS curr_longitude,
    g.amount_usd AS curr_amount_usd,
    g.channel AS curr_channel,
    g.entry_mode AS curr_entry_mode,
    g.merchant_id AS curr_merchant_id,
    LAG(g.auth_id) OVER card_time AS prev_auth_id,
    LAG(g.auth_timestamp) OVER card_time AS prev_auth_timestamp,
    LAG(g.city) OVER card_time AS prev_city,
    LAG(g.country_code) OVER card_time AS prev_country_code,
    LAG(g.latitude) OVER card_time AS prev_latitude,
    LAG(g.longitude) OVER card_time AS prev_longitude,
    LAG(g.merchant_id) OVER card_time AS prev_merchant_id
  FROM geo_auths AS g
  WINDOW card_time AS (PARTITION BY g.card_id ORDER BY g.auth_timestamp)
),
measured AS (
  SELECT
    p.prev_auth_id,
    p.curr_auth_id,
    p.prev_auth_timestamp,
    p.curr_auth_timestamp,
    p.prev_city,
    p.prev_country_code,
    p.curr_city,
    p.curr_country_code,
    p.prev_merchant_id,
    p.curr_merchant_id,
    p.curr_amount_usd,
    p.curr_channel,
    p.curr_entry_mode,
    ST_DISTANCE(
      ST_GEOGPOINT(p.prev_longitude, p.prev_latitude),
      ST_GEOGPOINT(p.curr_longitude, p.curr_latitude)
    ) / 1000.0 AS distance_km,
    TIMESTAMP_DIFF(p.curr_auth_timestamp, p.prev_auth_timestamp, SECOND) / 3600.0 AS elapsed_hours
  FROM paired AS p
  WHERE p.prev_auth_id IS NOT NULL
)
SELECT
  m.prev_auth_id,
  m.curr_auth_id,
  m.prev_auth_timestamp,
  m.curr_auth_timestamp,
  m.prev_city,
  m.prev_country_code,
  m.curr_city,
  m.curr_country_code,
  COALESCE(pm.merchant_name, 'UNKNOWN_MERCHANT') AS prev_merchant_name,
  COALESCE(cm.merchant_name, 'UNKNOWN_MERCHANT') AS curr_merchant_name,
  m.curr_amount_usd,
  m.curr_channel,
  m.curr_entry_mode,
  ROUND(m.distance_km, 1) AS distance_km,
  ROUND(m.elapsed_hours, 4) AS elapsed_hours,
  ROUND(SAFE_DIVIDE(m.distance_km, m.elapsed_hours), 1) AS implied_speed_kmh,
  @max_plausible_kmh AS max_plausible_kmh
FROM measured AS m
LEFT JOIN `abhinayboda-genai-bb.jpmc_ccb_fraud.merchants` AS pm
  ON m.prev_merchant_id = pm.merchant_id
LEFT JOIN `abhinayboda-genai-bb.jpmc_ccb_fraud.merchants` AS cm
  ON m.curr_merchant_id = cm.merchant_id
WHERE m.distance_km >= @min_distance_km
  AND (
    m.elapsed_hours <= 0
    OR SAFE_DIVIDE(m.distance_km, m.elapsed_hours) > @max_plausible_kmh
  )
ORDER BY implied_speed_kmh DESC NULLS FIRST, distance_km DESC;
