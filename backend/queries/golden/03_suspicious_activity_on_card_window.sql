-- =============================================================================
-- 03_suspicious_activity_on_card_window.sql
--
-- INTENT
--   Between these two moments, what happened on this card that a fraud analyst
--   would want to look at? This is the triage question. The customer says "I
--   think something happened while I was away last week" and the agent needs
--   the short list, not the full statement.
--
-- INPUTS
--   @card_id           STRING     e.g. 'CARD00013'
--   @window_start_ts   TIMESTAMP  inclusive lower bound
--   @window_end_ts     TIMESTAMP  inclusive upper bound
--   @min_risk_score    FLOAT64    0.0-1.0, e.g. 0.70
--
-- TABLES USED
--   abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations
--   abhinayboda-genai-bb.jpmc_ccb_fraud.merchants
--
-- WHY THIS IS THE APPROVED WAY
--   The big mistake a naive agent makes here is filtering on is_fraud. That
--   column is the ground-truth label we generated for scoring the demo, so an
--   agent that reads it is reading the answer key, and the triage looks
--   perfect for a reason that would not exist in production. This query never
--   touches is_fraud. It works from the same signals a real model would have
--   at the time: the model risk score, the risk band, the decline reasons, the
--   entry mode and whether the merchant is in a high-risk country.
--   Two smaller mistakes it also avoids. It does not throw away declines -
--   a run of declines is often the clearest fingerprint of card testing, where
--   someone probes a stolen number with small amounts until one goes through,
--   so declines are evidence rather than noise. And it returns a named reason
--   for every row it flags, in a flags array, because an agent that says "this
--   looks suspicious" without saying why cannot be checked by a human.
--
-- EXPECTED SHAPE
--   0 to a few dozen rows, worst first. One row per flagged authorization,
--   with a flags array naming each reason it was picked up and a
--   suspicion_reason_count giving how many fired.
-- =============================================================================

WITH windowed AS (
  SELECT
    a.auth_id,
    a.customer_id,
    a.card_id,
    a.token_id,
    a.auth_timestamp,
    a.local_timestamp,
    a.amount_usd,
    a.currency,
    a.channel,
    a.entry_mode,
    a.decision,
    a.decline_reason,
    a.risk_score,
    a.risk_band,
    a.city AS auth_city,
    a.country_code AS auth_country_code,
    a.merchant_id,
    COALESCE(m.merchant_name, 'UNKNOWN_MERCHANT') AS merchant_name,
    m.merchant_category,
    m.country_risk_tier,
    m.merchant_risk_score
  FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations` AS a
  LEFT JOIN `abhinayboda-genai-bb.jpmc_ccb_fraud.merchants` AS m
    ON a.merchant_id = m.merchant_id
  WHERE a.card_id = @card_id
    AND a.auth_timestamp >= @window_start_ts
    AND a.auth_timestamp <= @window_end_ts
),
flagged AS (
  SELECT
    w.auth_id,
    w.customer_id,
    w.card_id,
    w.token_id,
    w.auth_timestamp,
    w.local_timestamp,
    w.amount_usd,
    w.currency,
    w.channel,
    w.entry_mode,
    w.decision,
    w.decline_reason,
    w.risk_score,
    w.risk_band,
    w.auth_city,
    w.auth_country_code,
    w.merchant_id,
    w.merchant_name,
    w.merchant_category,
    w.country_risk_tier,
    w.merchant_risk_score,
    ARRAY(
      SELECT reason
      FROM UNNEST([
        IF(w.risk_score >= @min_risk_score, 'MODEL_RISK_SCORE_AT_OR_ABOVE_THRESHOLD', NULL),
        IF(w.risk_band IN ('HIGH', 'CRITICAL'), 'HIGH_OR_CRITICAL_RISK_BAND', NULL),
        IF(w.decision = 'DECLINED', 'DECLINED_AUTHORIZATION', NULL),
        IF(w.entry_mode = 'MAGSTRIPE', 'MAGSTRIPE_ENTRY_ON_A_CHIP_CARD_ERA_TRANSACTION', NULL),
        IF(w.channel = 'CARD_NOT_PRESENT' AND w.entry_mode = 'ECOM_KEYED', 'KEYED_CARD_NOT_PRESENT', NULL),
        IF(w.country_risk_tier = 'HIGH', 'MERCHANT_IN_HIGH_RISK_COUNTRY', NULL),
        IF(w.merchant_risk_score >= @min_risk_score, 'MERCHANT_RISK_SCORE_AT_OR_ABOVE_THRESHOLD', NULL),
        IF(w.merchant_id IS NOT NULL AND w.merchant_name = 'UNKNOWN_MERCHANT', 'MERCHANT_REFERENCE_NOT_IN_MERCHANT_TABLE', NULL),
        IF(w.amount_usd < 0, 'NEGATIVE_AMOUNT_NEEDS_DATA_REVIEW', NULL)
      ]) AS reason
      WHERE reason IS NOT NULL
    ) AS flags
  FROM windowed AS w
)
SELECT
  auth_id,
  auth_timestamp,
  local_timestamp,
  amount_usd,
  currency,
  channel,
  entry_mode,
  decision,
  decline_reason,
  risk_score,
  risk_band,
  auth_city,
  auth_country_code,
  merchant_id,
  merchant_name,
  merchant_category,
  country_risk_tier,
  flags,
  ARRAY_LENGTH(flags) AS suspicion_reason_count
FROM flagged
WHERE ARRAY_LENGTH(flags) > 0
ORDER BY suspicion_reason_count DESC, risk_score DESC, auth_timestamp DESC;
