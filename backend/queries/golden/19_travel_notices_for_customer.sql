-- =============================================================================
-- 19_travel_notices_for_customer.sql
--
-- INTENT
--   Every travel notice this customer has given the bank, in any state, with
--   the dates and destination. The assistant reads this before it explains a
--   declined charge abroad, confirms a trip, or ships a card to where the
--   customer says they are.
--
-- INPUTS
--   @customer_id  STRING   e.g. 'CUST00001'
--
-- TABLES USED
--   abhinayboda-genai-bb.jpmc_ccb_fraud.travel_notices
--
-- WHY THIS IS THE APPROVED WAY
--   The obvious query asks for status = 'ACTIVE', and status is exactly the
--   column that goes stale. A customer who comes home early still has a
--   notice reading ACTIVE with the original end date; the date they really
--   came back is in actual_return_date. So every notice is returned, whatever
--   its status, with actual_return_date next to the declared dates, and the
--   assistant reads the return date before it trusts the status or the
--   declared end.
--   A notice that was replaced (an extended trip, a changed destination) is
--   kept as SUPERSEDED and points at its replacement through
--   superseded_by_notice_id. Returning both, with the pointer, lets the
--   assistant follow the chain to the live notice instead of quoting the old
--   dates. CANCELLED notices are returned too, because "I cancelled that
--   trip" is something the assistant has to be able to confirm.
--   notes is deliberately left out. It is free text written by staff, not
--   something to read back to a customer.
--
-- EXPECTED SHAPE
--   0 to about 3 rows, one per notice, most recently declared first.
--   status is ACTIVE, EXPIRED, CANCELLED or SUPERSEDED.
-- =============================================================================

SELECT
  n.notice_id,
  n.status,
  n.declared_at,
  n.declared_channel,
  n.destination_country,
  n.destination_city,
  n.travel_start_date,
  n.travel_end_date,
  n.actual_return_date,
  n.superseded_by_notice_id
FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.travel_notices` AS n
WHERE n.customer_id = @customer_id
ORDER BY n.declared_at DESC, n.notice_id ASC;
