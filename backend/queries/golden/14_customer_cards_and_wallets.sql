-- =============================================================================
-- 14_customer_cards_and_wallets.sql
--
-- INTENT
--   Every card this customer has, whatever its state, with the account it
--   belongs to and the phones and watches that carry it. This answers "which
--   of my cards work right now?", "is my new card here yet?" and "which
--   devices can still pay with my old card?" in one read.
--
-- INPUTS
--   @customer_id  STRING   e.g. 'CUST00001'
--
-- TABLES USED
--   abhinayboda-genai-bb.jpmc_ccb_fraud.cards
--   abhinayboda-genai-bb.jpmc_ccb_fraud.accounts
--   abhinayboda-genai-bb.jpmc_ccb_fraud.wallet_tokens
--
-- WHY THIS IS THE APPROVED WAY
--   No other golden query lists a customer's cards. Query 07 only reaches
--   cards that have an open dispute and query 10 needs a card id up front, so
--   an assistant without this one ends up saying "you have one card" to a
--   customer holding five. Here nothing is filtered on status: ACTIVE, FROZEN,
--   REPLACED, PENDING_ACTIVATION and CLOSED all come back, physical and
--   virtual, because the question is usually about the card that is not
--   working. replaces_card_id and replaced_by_card_id are returned so the
--   assistant can walk the chain from the old plastic to the current one.
--   Cards are identified by last_four and nothing else. That is what a
--   customer recognises from their wallet, and it is the only card number
--   this query touches: the full and masked card number columns are never
--   selected.
--   Wallet tokens are rolled up into an array per card in their own step and
--   then joined on. Joining wallet_tokens straight onto cards returns one row
--   per token, and an assistant counting rows then reports three cards where
--   there is one card on three devices.
--   live_token_count uses the same rule as query 10: a token is live only when
--   token_status is ACTIVE and revoked_at is NULL. The two can disagree after
--   a half-finished revoke, and a frozen card with a live token is still a
--   card someone can spend on.
--   Cards are matched on the cardholder (cards.customer_id), not through the
--   account. On a joint account each owner has their own cards, and one owner
--   asking about "my cards" must not be shown the other owner's plastic or
--   devices.
--   The account is LEFT JOINed, so a card whose account row is missing still
--   shows up with empty account fields rather than disappearing.
--
-- EXPECTED SHAPE
--   0 to about 6 rows, one per card, newest card first (then card_id).
--   wallet_tokens is an array of (token_id, wallet_provider, device_name,
--   token_status, provisioned_at, provisioning_country, last_used_at,
--   revoked_at), oldest token first, and is empty when the card has none.
-- =============================================================================

WITH tokens_by_card AS (
  SELECT
    t.card_id,
    COUNTIF(t.token_status = 'ACTIVE' AND t.revoked_at IS NULL) AS live_token_count,
    ARRAY_AGG(
      STRUCT(
        t.token_id,
        t.wallet_provider,
        t.device_name,
        t.token_status,
        t.provisioned_at,
        t.provisioning_country,
        t.last_used_at,
        t.revoked_at
      )
      ORDER BY t.provisioned_at ASC, t.token_id ASC
    ) AS wallet_tokens
  FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.wallet_tokens` AS t
  WHERE t.card_id IN (
    SELECT card_id
    FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.cards`
    WHERE customer_id = @customer_id
  )
  GROUP BY t.card_id
)
SELECT
  c.card_id,
  c.account_id,
  a.account_type,
  a.product_name,
  a.regulation_path,
  c.last_four,
  c.card_type,
  c.network,
  c.status,
  c.freeze_reason,
  c.issue_date,
  c.expiry_date,
  c.replaces_card_id,
  c.replaced_by_card_id,
  COALESCE(tk.live_token_count, 0) AS live_token_count,
  tk.wallet_tokens
FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.cards` AS c
LEFT JOIN `abhinayboda-genai-bb.jpmc_ccb_fraud.accounts` AS a
  ON c.account_id = a.account_id
LEFT JOIN tokens_by_card AS tk
  ON c.card_id = tk.card_id
WHERE c.customer_id = @customer_id
ORDER BY c.issue_date DESC, c.card_id ASC;
