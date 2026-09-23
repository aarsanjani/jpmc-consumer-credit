-- =============================================================================
-- 10_wallet_tokens_active_on_card.sql
--
-- INTENT
--   Which phones and watches can still spend on this card? A digital wallet
--   token is the stand-in number a device uses instead of the card number, so
--   freezing the plastic card does nothing to a live token. Before the agent
--   tells a customer their card is safe, it has to list what else is still
--   attached and offer to revoke it.
--
-- INPUTS
--   @card_id             STRING     e.g. 'CARD00013'
--   @as_of_ts            TIMESTAMP  the "now" the agent reasons from
--   @usage_lookback_days INT64      recent-usage window, e.g. 30
--   @include_inactive    BOOL       FALSE for live tokens only, TRUE to show
--                                   suspended and revoked ones too
--
-- TABLES USED
--   {dataset}.wallet_tokens
--   {dataset}.cards
--   {dataset}.authorizations
--
-- WHY THIS IS THE APPROVED WAY
--   The mistake that matters here is treating the card freeze as the end of
--   the job. Tokens are separate credentials with their own status, so a card
--   with status FROZEN can still have three ACTIVE tokens on three devices.
--   This query always reports the card status next to the token status so the
--   gap is visible in one look, and the agent cannot claim the card is locked
--   down while a token is still live.
--   Second, "active" has to mean token_status = 'ACTIVE' and revoked_at IS
--   NULL together. Those two can disagree if a revoke half-completed, and an
--   agent trusting only the status string would miss it. Both are checked and
--   both are returned.
--   Third, last_used_at on the token row is maintained by the wallet provider
--   feed and can lag. So the query also counts what the token actually spent
--   in the last @usage_lookback_days straight from authorizations, and returns
--   that beside last_used_at. A token that claims it has not been used for a
--   month but has twelve authorizations this week is the interesting one.
--   The aggregate is computed in its own step and joined on, rather than
--   joined row by row, because joining wallet_tokens straight to
--   authorizations multiplies the token rows by their transactions and an
--   agent counting rows would report four tokens when there is one.
--   Card identity is shown as pan_masked and last_four. pan_full is restricted
--   and is never selected.
--
-- EXPECTED SHAPE
--   0 to about 5 rows, one per token on the card, live tokens first then most
--   recently used. device_trust_score is 0.0 to 1.0 where higher is more
--   trusted.
-- =============================================================================

WITH token_usage AS (
  SELECT
    a.token_id,
    COUNT(*) AS auth_count_lookback,
    COUNTIF(a.decision = 'APPROVED') AS approved_count_lookback,
    COUNTIF(a.decision = 'DECLINED') AS declined_count_lookback,
    SUM(IF(a.decision = 'APPROVED', a.amount_usd, NUMERIC '0')) AS approved_amount_usd_lookback,
    MAX(a.auth_timestamp) AS last_observed_auth_timestamp
  FROM `{dataset}.authorizations` AS a
  WHERE a.card_id = @card_id
    AND a.token_id IS NOT NULL
    AND a.auth_timestamp > TIMESTAMP_SUB(@as_of_ts, INTERVAL @usage_lookback_days DAY)
    AND a.auth_timestamp <= @as_of_ts
  GROUP BY a.token_id
)
SELECT
  t.token_id,
  t.card_id,
  t.customer_id,
  c.pan_masked,
  c.last_four,
  c.card_type,
  c.network,
  c.status AS card_status,
  c.freeze_reason AS card_freeze_reason,
  t.wallet_provider,
  t.device_id,
  t.device_name,
  t.device_os,
  t.device_trust_score,
  t.token_status,
  (t.token_status = 'ACTIVE' AND t.revoked_at IS NULL) AS token_is_live,
  (t.token_status = 'ACTIVE' AND t.revoked_at IS NOT NULL) AS token_status_disagrees_with_revoked_at,
  t.provisioned_at,
  t.provisioning_country,
  t.last_used_at,
  t.revoked_at,
  COALESCE(u.auth_count_lookback, 0) AS auth_count_lookback,
  COALESCE(u.approved_count_lookback, 0) AS approved_count_lookback,
  COALESCE(u.declined_count_lookback, 0) AS declined_count_lookback,
  COALESCE(u.approved_amount_usd_lookback, NUMERIC '0') AS approved_amount_usd_lookback,
  u.last_observed_auth_timestamp,
  (c.status IN ('FROZEN', 'CLOSED') AND t.token_status = 'ACTIVE' AND t.revoked_at IS NULL)
    AS live_token_on_a_locked_card
FROM `{dataset}.wallet_tokens` AS t
JOIN `{dataset}.cards` AS c
  ON t.card_id = c.card_id
LEFT JOIN token_usage AS u
  ON t.token_id = u.token_id
WHERE t.card_id = @card_id
  AND (@include_inactive OR (t.token_status = 'ACTIVE' AND t.revoked_at IS NULL))
ORDER BY token_is_live DESC, t.last_used_at DESC NULLS LAST, t.token_id ASC;
