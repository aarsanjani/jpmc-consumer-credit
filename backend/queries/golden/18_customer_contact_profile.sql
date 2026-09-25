-- =============================================================================
-- 18_customer_contact_profile.sql
--
-- INTENT
--   Who is this customer, where do they live, and how do they want to be
--   reached? The assistant needs this to greet the customer by name, confirm
--   the address before a card is shipped anywhere, and respect how and when
--   they have asked to be contacted.
--
-- INPUTS
--   @customer_id  STRING  e.g. 'CUST00001'
--   @as_of_date   DATE    the demo's "today", used only to say whether the
--                         temporary address is in effect, e.g. DATE '2026-09-22'
--
-- TABLES USED
--   abhinayboda-genai-bb.jpmc_ccb_fraud.customers
--
-- WHY THIS IS THE APPROVED WAY
--   Addresses and contact preferences only live in the raw customers table,
--   which also holds a restricted identity column. So this query names every
--   column it reads and never uses SELECT *: the restricted column cannot come
--   along by accident, and adding a column to the table does not quietly add
--   it to what the assistant sees.
--   The phone number never leaves BigQuery in full. The catalog marks
--   phone_primary MASK, and only its last four digits are returned. That is
--   enough for "is this still the number ending 1234?" and useless to anyone
--   trying to take over the account, and a number the assistant never
--   receives is a number it cannot read out to the wrong caller.
--   The temporary address comes back with its validity dates and
--   temp_address_in_effect, which is TRUE only when @as_of_date falls inside
--   them (NULL if the dates are missing, FALSE when there is no temporary
--   address). Without it an assistant ships a card to a holiday address that
--   lapsed a week ago. It only reflects the dates on file: a customer can come
--   home early (query 19 shows when they actually returned), so the assistant
--   still confirms the address with the customer before anything ships.
--   standing_instructions is the customer's own free-text instruction to the
--   bank (for example "text me first"). It is returned verbatim because it is
--   theirs, and the assistant is expected to follow it.
--
-- EXPECTED SHAPE
--   Exactly 0 or 1 rows. 0 means the customer id does not exist, which the
--   assistant should report as "not found" rather than guessing.
-- =============================================================================

SELECT
  c.customer_id,
  ARRAY_TO_STRING([c.first_name, c.last_name], ' ') AS full_name,
  c.first_name,
  c.home_address_line1,
  c.home_city,
  c.home_state,
  c.home_postal,
  c.home_country,
  c.temp_address_line1,
  c.temp_city,
  c.temp_country,
  c.temp_valid_from,
  c.temp_valid_to,
  (c.temp_address_line1 IS NOT NULL AND @as_of_date BETWEEN c.temp_valid_from AND c.temp_valid_to)
    AS temp_address_in_effect,
  c.preferred_channel,
  c.preferred_language,
  RIGHT(c.phone_primary, 4) AS phone_last4,
  c.standing_instructions
FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.customers` AS c
WHERE c.customer_id = @customer_id;
