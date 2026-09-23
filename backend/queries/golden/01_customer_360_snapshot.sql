-- =============================================================================
-- 01_customer_360_snapshot.sql
--
-- INTENT
--   Who is this customer and what does their last 90 days look like, in one
--   row? An agent asks this first, before it does anything else, so it knows
--   the segment, the standing instructions, whether they are travelling, how
--   many disputes are already open, and how to reach them.
--
-- INPUTS
--   @customer_id  STRING   e.g. 'CUST00007'
--
-- TABLES USED
--   {dataset}.v_customer_360   (view)
--
-- WHY THIS IS THE APPROVED WAY
--   The view is the only safe front door, because it is built without
--   ssn_last4 and without pan_full, so an agent using it cannot accidentally
--   print identity data or a full card number. An agent writing its own
--   version would join customers to accounts to cards to authorizations to
--   disputes by hand, and there are three ways that goes wrong: it fans out
--   the row count (one customer times many cards times many authorizations)
--   and reports inflated counts; it silently counts closed accounts and
--   revoked tokens as active because it forgets the status filters; and it
--   reaches into the raw customers table where the restricted columns live.
--   The view already applies the 90-day boundary and the status filters once,
--   in one place, so every agent gets the same numbers.
--
-- EXPECTED SHAPE
--   Exactly 0 or 1 rows. 0 means the customer id does not exist, which the
--   agent should report as "not found" rather than guessing.
--   Columns: customer_id, full_name, segment, preferred_channel,
--   standing_instructions, open_accounts, active_cards, active_tokens,
--   auth_count_90d, approved_amount_usd_90d, declined_count_90d,
--   fraud_auth_count_90d, open_disputes, total_disputed_amount,
--   active_travel_country, last_contact_at, last_contact_channel,
--   contact_count_90d.
-- =============================================================================

SELECT
  customer_id,
  full_name,
  segment,
  preferred_channel,
  standing_instructions,
  open_accounts,
  active_cards,
  active_tokens,
  auth_count_90d,
  approved_amount_usd_90d,
  declined_count_90d,
  fraud_auth_count_90d,
  open_disputes,
  total_disputed_amount,
  active_travel_country,
  last_contact_at,
  last_contact_channel,
  contact_count_90d
FROM `{dataset}.v_customer_360`
WHERE customer_id = @customer_id;
