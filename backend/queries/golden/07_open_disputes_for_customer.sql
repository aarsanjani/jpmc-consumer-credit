-- =============================================================================
-- 07_open_disputes_for_customer.sql
--
-- INTENT
--   What disputes does this customer already have running, and where does each
--   one stand? The agent needs this before it opens anything new, because
--   opening a second dispute on a charge that is already being investigated is
--   a real operational error - it duplicates the case, can double the
--   provisional credit, and confuses the customer.
--
-- INPUTS
--   @customer_id     STRING   e.g. 'CUST00007'
--   @as_of_date      DATE     the date the agent is reasoning from, e.g.
--                             DATE '2026-09-21'
--   @open_statuses   ARRAY<STRING>  which statuses count as still open, e.g.
--                             ['OPEN','UNDER_REVIEW','PROVISIONAL_CREDIT_ISSUED']
--
-- TABLES USED
--   abhinayboda-genai-bb.jpmc_ccb_fraud.disputes
--   abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations
--   abhinayboda-genai-bb.jpmc_ccb_fraud.merchants
--   abhinayboda-genai-bb.jpmc_ccb_fraud.cards
--
-- WHY THIS IS THE APPROVED WAY
--   "Open" is not obvious, and that is the whole point of fixing it here. A
--   dispute sitting at PROVISIONAL_CREDIT_ISSUED is still open - the money is
--   with the customer but the investigation has not finished and the credit
--   can still be pulled back. An agent that treats that status as closed will
--   tell the customer the case is done when it is not. WITHDRAWN, on the other
--   hand, is closed even though nobody resolved anything. Passing the status
--   list as a parameter keeps that definition in one place instead of hard
--   coding someone's guess.
--   The query joins to the disputed authorization so the agent can name the
--   merchant and the date without a second call, which matters because the
--   customer never remembers the dispute id, only "the charge from that hotel".
--   It reports card identity with pan_masked and last_four only. pan_full is a
--   restricted column and is never selected, and last_four is enough for a
--   customer to confirm which card is which.
--   It also surfaces the two clocks that actually matter - the provisional
--   credit due date and the investigation due date - with how many days are
--   left on each as of @as_of_date, rather than leaving an agent to subtract
--   dates in its head, which is where date errors creep in.
--
-- EXPECTED SHAPE
--   0 to about 5 rows for a normal customer, oldest dispute first so the most
--   overdue case is read out first. One row per open dispute.
-- =============================================================================

SELECT
  d.dispute_id,
  d.customer_id,
  d.account_id,
  d.card_id,
  c.pan_masked,
  c.last_four,
  c.status AS card_status,
  d.auth_id,
  a.auth_timestamp,
  COALESCE(m.merchant_name, 'UNKNOWN_MERCHANT') AS merchant_name,
  m.merchant_category,
  d.opened_date,
  d.opened_channel,
  d.regulation_path,
  d.dispute_reason_code,
  d.dispute_reason,
  d.disputed_amount,
  d.status,
  d.assigned_queue,
  d.provisional_credit_issued,
  d.provisional_credit_date,
  d.provisional_credit_amount,
  d.provisional_credit_due_date,
  d.investigation_due_date,
  d.days_since_transaction,
  d.filing_window_days,
  d.is_within_filing_window,
  DATE_DIFF(@as_of_date, d.opened_date, DAY) AS days_open,
  DATE_DIFF(d.provisional_credit_due_date, @as_of_date, DAY) AS days_until_provisional_credit_due,
  DATE_DIFF(d.investigation_due_date, @as_of_date, DAY) AS days_until_investigation_due,
  (NOT d.provisional_credit_issued AND d.provisional_credit_due_date < @as_of_date) AS provisional_credit_overdue,
  (d.resolved_date IS NULL AND d.investigation_due_date < @as_of_date) AS investigation_overdue
FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.disputes` AS d
LEFT JOIN `abhinayboda-genai-bb.jpmc_ccb_fraud.authorizations` AS a
  ON d.auth_id = a.auth_id
LEFT JOIN `abhinayboda-genai-bb.jpmc_ccb_fraud.merchants` AS m
  ON a.merchant_id = m.merchant_id
LEFT JOIN `abhinayboda-genai-bb.jpmc_ccb_fraud.cards` AS c
  ON d.card_id = c.card_id
WHERE d.customer_id = @customer_id
  AND d.status IN UNNEST(@open_statuses)
ORDER BY d.opened_date ASC, d.dispute_id ASC;
