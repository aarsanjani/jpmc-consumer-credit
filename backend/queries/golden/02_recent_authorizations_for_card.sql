-- =============================================================================
-- 02_recent_authorizations_for_card.sql
--
-- INTENT
--   Show me the most recent card transactions on one card, newest first, with
--   the merchant name attached. This is the "read me my charges" question a
--   customer asks on the phone, and the list an agent reads back to them.
--
-- INPUTS
--   @card_id        STRING   e.g. 'CARD00013'
--   @as_of_ts       TIMESTAMP  the "now" the agent is reasoning from, e.g.
--                              TIMESTAMP '2026-09-21 00:00:00+00'
--   @lookback_days  INT64    how far back to look, e.g. 30
--   @row_limit      INT64    how many rows to return, e.g. 50
--
-- TABLES USED
--   abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations
--   abhinayboda-genai-bb.jpmc_ccb_fraud.merchants
--
-- WHY THIS IS THE APPROVED WAY
--   Three things a hand-written version gets wrong.
--   First, it joins merchants with an inner join, which quietly drops any
--   authorization whose merchant_id has no matching merchant row. There is a
--   deliberately orphaned merchant reference in this dataset, so an inner join
--   makes a real transaction vanish from the customer's statement, and the
--   agent then tells the customer a charge does not exist when it does. We use
--   a LEFT JOIN and label the missing merchant instead of hiding the row.
--   Second, it filters on created_at or settled_date. Those are load-time and
--   posting-time fields, not the time the card was used, so the window comes
--   out wrong. auth_timestamp is the event time and is the only correct filter.
--   Third, it pulls pan_full from cards to "identify" the card. We never do
--   that; pan_masked and last_four identify a card perfectly well for a human,
--   and pan_full is a restricted column.
--   The query is also bounded on both time and row count, because an unbounded
--   scan of an 18,000-row fact table costs money and returns more than an
--   agent can reason over.
--
-- EXPECTED SHAPE
--   0 to @row_limit rows, ordered newest first. One row per authorization.
--   merchant_name is the literal string 'UNKNOWN_MERCHANT' when the merchant
--   reference is orphaned, which is itself a signal worth surfacing.
-- =============================================================================

SELECT
  a.auth_id,
  a.auth_timestamp,
  a.local_timestamp,
  a.amount,
  a.currency,
  a.amount_usd,
  a.posted_amount,
  a.channel,
  a.entry_mode,
  a.decision,
  a.decline_reason,
  a.risk_score,
  a.risk_band,
  a.is_settled,
  a.settled_date,
  a.city AS auth_city,
  a.country_code AS auth_country_code,
  a.token_id,
  a.merchant_id,
  COALESCE(m.merchant_name, 'UNKNOWN_MERCHANT') AS merchant_name,
  m.merchant_category,
  m.mcc,
  m.mcc_description,
  m.country_code AS merchant_country_code,
  m.country_risk_tier,
  (m.merchant_id IS NULL) AS merchant_reference_is_orphaned
FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations` AS a
LEFT JOIN `abhinayboda-genai-bb.jpmc_ccb_fraud.merchants` AS m
  ON a.merchant_id = m.merchant_id
WHERE a.card_id = @card_id
  AND a.auth_timestamp > TIMESTAMP_SUB(@as_of_ts, INTERVAL @lookback_days DAY)
  AND a.auth_timestamp <= @as_of_ts
ORDER BY a.auth_timestamp DESC
LIMIT @row_limit;
