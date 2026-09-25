-- =============================================================================
-- 13_customer_accounts_and_balances.sql
--
-- INTENT
--   What accounts does this customer hold, and what is the balance on each?
--   This is the first thing a customer asks a chat assistant ("how much is in
--   my checking?", "what's my credit limit?"), and the list the assistant
--   reads back before it talks about any one account.
--
-- INPUTS
--   @customer_id  STRING   e.g. 'CUST00001'
--
-- TABLES USED
--   abhinayboda-genai-bb.jpmc_ccb_fraud.accounts
--
-- WHY THIS IS THE APPROVED WAY
--   A customer can own an account in two ways: as the primary owner
--   (customer_id) or as the second owner on a joint account
--   (joint_customer_id). The obvious query filters on customer_id alone, and a
--   joint owner is then told they have no checking account when they do. This
--   query matches either column and labels each row PRIMARY or JOINT, so the
--   assistant knows when it is talking to a co-owner.
--   It never returns joint_customer_id itself. The catalog marks that column
--   ALLOW_AGGREGATE_ONLY: it may be used to find the account, but the other
--   owner's id is not this customer's data and is not read back to them.
--   credit_limit is NULL on checking and savings accounts because they have no
--   limit. It is returned as NULL, never as 0: "no limit applies" and "your
--   limit is $0" are different answers, and a zero here would be a number
--   nobody measured.
--   current_balance includes transactions that have not settled yet;
--   available_balance is what the customer can spend right now. Both are
--   returned so the assistant can explain a gap between them instead of
--   picking one and being wrong half the time.
--   regulation_path says which dispute rules the account falls under (REG_E
--   for deposit accounts, REG_Z for credit cards), so a later dispute answer
--   can start from the right rulebook.
--   Every status is returned (OPEN, CLOSED, RESTRICTED), because "why can't I
--   use this account?" is a question about a non-open one.
--
-- EXPECTED SHAPE
--   0 to about 5 rows, one per account, ordered by account_id. 0 rows means
--   the customer holds no accounts or does not exist; the assistant should
--   say so rather than guess.
-- =============================================================================

SELECT
  a.account_id,
  a.account_type,
  a.product_name,
  a.status,
  a.currency,
  a.credit_limit,
  a.current_balance,
  a.available_balance,
  a.regulation_path,
  a.open_date,
  IF(a.customer_id = @customer_id, 'PRIMARY', 'JOINT') AS ownership
FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.accounts` AS a
WHERE a.customer_id = @customer_id
  OR a.joint_customer_id = @customer_id
ORDER BY a.account_id;
