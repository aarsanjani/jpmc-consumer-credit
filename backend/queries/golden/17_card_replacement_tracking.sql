-- =============================================================================
-- 17_card_replacement_tracking.sql
--
-- INTENT
--   Where is this customer's replacement card, and what has the bank done
--   about the card it replaces? One row per replaced card and shipment: when
--   the replacement was ordered, whether a virtual card was issued in the
--   meantime, the latest shipping status, where it is going, who is carrying
--   it, and which new card is on its way and whether it is activated yet.
--   This is "where's my new card?" in a customer chat.
--
-- INPUTS
--   @customer_id  STRING   e.g. 'CUST00001'
--
-- TABLES USED
--   abhinayboda-genai-bb.jpmc_ccb_fraud.dispute_events
--   abhinayboda-genai-bb.jpmc_ccb_fraud.cards
--
-- WHY THIS IS THE APPROVED WAY
--   Replacements, virtual cards and shipments are things the bank does to a
--   card, not to a dispute, so they are logged with a NULL dispute_id. The
--   obvious query goes through the dispute and finds nothing. This one keys
--   on the customer and then on the card.
--   A shipment is several events that share a tracking number - dispatched,
--   sometimes rerouted, delivered - and its current status is the LATEST of
--   them, not the first one found. Reading the dispatch row tells a customer
--   their card is going to their home address after it was rerouted abroad.
--   So status and destination both come from the newest event per tracking
--   number, with event_id breaking timestamp ties so the answer is stable.
--   The carrier and service only exist inside the dispatch event's JSON
--   event_detail, and signed_for_by only inside the delivery event's, so
--   they are read from there with JSON_VALUE. A key that is missing comes
--   back NULL rather than failing the query.
--   The card on the event is the card being replaced. The new card is found
--   through that card's replaced_by_card_id, and its status is returned
--   because PENDING_ACTIVATION means "delivered, but you still have to
--   activate it", which is usually the real answer to "my new card doesn't
--   work".
--   Both card joins also require the card to belong to this customer. In this
--   dataset most customers' events carry a card id that belongs to a
--   different customer, and joining on card_id alone would read another
--   person's card number and status out to this one. When the card is not on
--   the customer's record, card_on_customer_record is FALSE and every card
--   field is NULL, so the assistant can say it cannot match the shipment to a
--   card instead of naming someone else's.
--   An order that has not shipped yet still gets a row, with no tracking
--   number, so "ordered, not dispatched" is visible rather than absent.
--   Order and virtual-card details are per replaced card; if one card were
--   ever replaced twice, both of its shipments would show the latest order.
--   The dataset has no such card; pairing orders to shipments by time is the
--   fix if one appears.
--
-- EXPECTED SHAPE
--   0 to about 3 rows, most recent activity first. shipment_status is the
--   latest event type for the tracking number (SHIPMENT_DISPATCHED,
--   SHIPMENT_REROUTED or SHIPMENT_DELIVERED) and is NULL when nothing has
--   shipped. destination_type is HOME, TEMPORARY or BRANCH.
-- =============================================================================

WITH replacement_events AS (
  SELECT
    e.event_id,
    e.card_id,
    e.event_timestamp,
    e.event_type,
    e.shipment_tracking_id,
    e.shipment_destination_type,
    e.shipment_destination_country,
    JSON_VALUE(e.event_detail, '$.carrier') AS carrier,
    JSON_VALUE(e.event_detail, '$.service') AS carrier_service,
    JSON_VALUE(e.event_detail, '$.signed_for_by') AS signed_for_by,
    SAFE_CAST(JSON_VALUE(e.event_detail, '$.expedited') AS BOOL) AS expedited,
    JSON_VALUE(e.event_detail, '$.delivery') AS delivery
  FROM `abhinayboda-genai-bb.jpmc_ccb_fraud.dispute_events` AS e
  WHERE e.customer_id = @customer_id
    AND e.event_type IN (
      'REPLACEMENT_ORDERED',
      'VIRTUAL_CARD_ISSUED',
      'SHIPMENT_DISPATCHED',
      'SHIPMENT_REROUTED',
      'SHIPMENT_DELIVERED'
    )
),
card_actions AS (
  SELECT
    card_id,
    MAX(IF(event_type = 'REPLACEMENT_ORDERED', event_timestamp, NULL)) AS replacement_ordered_at,
    LOGICAL_OR(IF(event_type = 'REPLACEMENT_ORDERED', expedited, NULL)) AS replacement_expedited,
    MAX(IF(event_type = 'VIRTUAL_CARD_ISSUED', event_timestamp, NULL)) AS virtual_card_issued_at,
    MAX(IF(event_type = 'VIRTUAL_CARD_ISSUED', delivery, NULL)) AS virtual_card_delivery
  FROM replacement_events
  GROUP BY card_id
),
shipments AS (
  SELECT
    card_id,
    shipment_tracking_id,
    ARRAY_AGG(
      STRUCT(event_type, event_timestamp, shipment_destination_type, shipment_destination_country)
      ORDER BY event_timestamp DESC, event_id DESC
      LIMIT 1
    )[OFFSET(0)] AS latest,
    MIN(IF(event_type = 'SHIPMENT_DISPATCHED', event_timestamp, NULL)) AS dispatched_at,
    MAX(IF(event_type = 'SHIPMENT_REROUTED', event_timestamp, NULL)) AS last_rerouted_at,
    MAX(IF(event_type = 'SHIPMENT_DELIVERED', event_timestamp, NULL)) AS delivered_at,
    MAX(IF(event_type = 'SHIPMENT_DISPATCHED', carrier, NULL)) AS carrier,
    MAX(IF(event_type = 'SHIPMENT_DISPATCHED', carrier_service, NULL)) AS carrier_service,
    MAX(IF(event_type = 'SHIPMENT_DELIVERED', signed_for_by, NULL)) AS signed_for_by
  FROM replacement_events
  WHERE shipment_tracking_id IS NOT NULL
  GROUP BY card_id, shipment_tracking_id
)
SELECT
  s.shipment_tracking_id,
  s.latest.event_type AS shipment_status,
  s.latest.event_timestamp AS shipment_status_at,
  s.latest.shipment_destination_type AS destination_type,
  s.latest.shipment_destination_country AS destination_country,
  s.carrier,
  s.carrier_service,
  s.dispatched_at,
  s.last_rerouted_at,
  s.delivered_at,
  s.signed_for_by,
  ca.replacement_ordered_at,
  ca.replacement_expedited,
  ca.virtual_card_issued_at,
  ca.virtual_card_delivery,
  (old_card.card_id IS NOT NULL) AS card_on_customer_record,
  old_card.card_id AS replaced_card_id,
  old_card.last_four AS replaced_card_last_four,
  old_card.status AS replaced_card_status,
  new_card.card_id AS replacement_card_id,
  new_card.last_four AS replacement_card_last_four,
  new_card.card_type AS replacement_card_type,
  new_card.status AS replacement_card_status,
  new_card.issue_date AS replacement_card_issue_date
FROM card_actions AS ca
LEFT JOIN shipments AS s
  ON ca.card_id = s.card_id
LEFT JOIN `abhinayboda-genai-bb.jpmc_ccb_fraud.cards` AS old_card
  ON ca.card_id = old_card.card_id
  AND old_card.customer_id = @customer_id
LEFT JOIN `abhinayboda-genai-bb.jpmc_ccb_fraud.cards` AS new_card
  ON old_card.replaced_by_card_id = new_card.card_id
  AND new_card.customer_id = @customer_id
ORDER BY
  COALESCE(s.latest.event_timestamp, ca.replacement_ordered_at, ca.virtual_card_issued_at) DESC,
  s.shipment_tracking_id ASC;
