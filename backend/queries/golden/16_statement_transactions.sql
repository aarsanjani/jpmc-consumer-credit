-- =============================================================================
-- 16_statement_transactions.sql
--
-- INTENT
--   The line items on one account for a statement period, newest first, with
--   the period totals on every row. This is "show me my statement", "what's
--   this charge from Tuesday?" and "has it posted yet?" in a customer chat.
--
-- INPUTS
--   @customer_id   STRING  the customer asking, e.g. 'CUST00001'
--   @account_id    STRING  e.g. 'ACCT000003'
--   @period_start  DATE    inclusive, UTC calendar day, e.g. DATE '2026-08-24'
--   @period_end    DATE    inclusive, UTC calendar day, e.g. DATE '2026-09-22'
--   @row_limit     INT64   cap on line items returned, e.g. 200
--
-- TABLES USED
--   abhinayboda-genai-bb.jpmc_ccb_fraud.accounts
--   abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations
--   abhinayboda-genai-bb.jpmc_ccb_fraud.merchants
--
-- WHY THIS IS THE APPROVED WAY
--   The account has to belong to the customer asking, as primary or joint
--   owner, or nothing comes back. Without that check any account id typed
--   into a chat returns someone else's statement. A joint owner sees the
--   whole account, which is what a joint statement is.
--   Line items are selected by account, not by card. One account can have
--   several cards at once (a frozen card, its replacement and a virtual card
--   issued in between), and a statement filtered on one card silently drops
--   the spend on the others.
--   The list is capped at @row_limit rows (the wrapper passes 200), because a
--   busy month runs to hundreds of rows. The totals are NOT computed from the
--   capped list: they come from their own step over the whole period, so they
--   stay right when the list is cut. line_items_in_period against the number
--   of rows returned tells the assistant whether the cap kicked in.
--   Declined attempts are included and labelled by decision, because
--   customers ask about charges that "didn't go through". They never count
--   towards approved_amount_usd.
--   amount is in the transaction's own currency and amount_usd is its US
--   dollar value; both are returned so a euro purchase reads as euros and the
--   totals still add up in dollars. posted_amount is what actually hit the
--   account, which can differ from the authorized amount because of tips or
--   exchange-rate movement; for foreign-currency rows it tracks amount_usd,
--   not amount, so it is in the account's currency. It is NULL until the
--   transaction settles, and pending_count counts approved rows that have not
--   settled yet.
--   Merchants are LEFT JOINed and an orphaned merchant reference is shown as
--   UNKNOWN_MERCHANT, so a real charge never drops off the statement.
--   The period is filtered on auth_timestamp, the time the card was used, by
--   UTC calendar day. Ordering is newest first with auth_id as the tie-break,
--   so the same period always lists in the same order.
--
-- EXPECTED SHAPE
--   0 to @row_limit rows, one per authorization, newest first. Every row
--   carries the same period totals: line_items_in_period, approved_count,
--   approved_amount_usd, declined_count, pending_count, posted_amount_total.
--   0 rows means the account is not this customer's or had no activity in the
--   period; query 13 tells the two apart.
-- =============================================================================

WITH owned_account AS (
  SELECT
    a.account_id,
    a.currency AS account_currency
  FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.accounts` AS a
  WHERE a.account_id = @account_id
    AND (a.customer_id = @customer_id OR a.joint_customer_id = @customer_id)
),
period_auths AS (
  SELECT
    au.auth_id,
    au.card_id,
    au.auth_timestamp,
    au.merchant_id,
    au.city,
    au.country_code,
    au.amount,
    au.currency,
    au.amount_usd,
    au.decision,
    au.is_settled,
    au.posted_amount,
    oa.account_currency
  FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations` AS au
  JOIN owned_account AS oa
    ON au.account_id = oa.account_id
  WHERE DATE(au.auth_timestamp, 'UTC') BETWEEN @period_start AND @period_end
),
period_totals AS (
  SELECT
    COUNT(*) AS line_items_in_period,
    COUNTIF(decision = 'APPROVED') AS approved_count,
    COALESCE(SUM(IF(decision = 'APPROVED', amount_usd, NULL)), NUMERIC '0') AS approved_amount_usd,
    COUNTIF(decision = 'DECLINED') AS declined_count,
    COUNTIF(decision = 'APPROVED' AND is_settled IS NOT TRUE) AS pending_count,
    COALESCE(SUM(IF(is_settled, posted_amount, NULL)), NUMERIC '0') AS posted_amount_total
  FROM period_auths
)
SELECT
  p.auth_id,
  p.card_id,
  p.auth_timestamp,
  DATE(p.auth_timestamp, 'UTC') AS transaction_date,
  COALESCE(m.merchant_name, 'UNKNOWN_MERCHANT') AS merchant_name,
  m.merchant_category,
  p.city,
  p.country_code,
  p.amount,
  p.currency,
  p.amount_usd,
  p.decision,
  p.is_settled,
  p.posted_amount,
  p.account_currency,
  t.line_items_in_period,
  t.approved_count,
  t.approved_amount_usd,
  t.declined_count,
  t.pending_count,
  t.posted_amount_total
FROM period_auths AS p
LEFT JOIN `abhinayboda-genai-bb.jpmc_ccb_fraud.merchants` AS m
  ON p.merchant_id = m.merchant_id
CROSS JOIN period_totals AS t
ORDER BY p.auth_timestamp DESC, p.auth_id DESC
LIMIT @row_limit;
