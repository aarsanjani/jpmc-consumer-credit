-- =============================================================================
-- 11_dispute_event_timeline.sql
--
-- INTENT
--   Tell me everything that happened on this case, in order. The customer asks
--   "so what have you actually done?" and the agent needs one ordered story:
--   dispute opened, card frozen, tokens revoked, virtual card issued,
--   replacement ordered, shipment dispatched, rerouted, delivered, resolved.
--
-- INPUTS
--   @dispute_id        STRING     e.g. 'DSP00012'
--   @as_of_date        DATE       used as the end of the case window when the
--                                 dispute is not resolved yet
--   @pre_open_hours    INT64      how far before the dispute opened to pick up
--                                 related card actions, e.g. 72
--   @post_close_hours  INT64      how far after it closed to keep looking,
--                                 e.g. 168
--
-- TABLES USED
--   {dataset}.disputes
--   {dataset}.dispute_events
--
-- WHY THIS IS THE APPROVED WAY
--   The obvious query is "select from dispute_events where dispute_id = X" and
--   it gives an incomplete and misleading story. Card actions - freezing the
--   card, revoking a wallet token, issuing a virtual card - are logged with a
--   null dispute_id, because the bank does them to the card, not to the case.
--   So the obvious query returns "dispute opened, evidence requested,
--   resolved" and the agent tells the customer nothing was done about the
--   card, when in fact it was frozen four minutes in. This query pulls those
--   card-linked events in too, bounded to the same card and the window around
--   the case, and labels each row DISPUTE or CARD_ACTION so a reader can tell
--   which stream it came from.
--   The window bound is why the pre and post hours are parameters rather than
--   "all events on this card ever". Without a bound you get every freeze the
--   card has ever had, including ones from a different incident six weeks
--   earlier, and the timeline stops being a timeline.
--   Ordering is by event_timestamp then event_id. The tie-break on event_id
--   matters because several events can share a timestamp to the second, and
--   the demo grades the agent on getting the sequence right - freeze before
--   replacement, token revoke before or with the freeze, virtual card before
--   the physical one, reroute only after a dispatch. An unstable sort would
--   make the same query look correct on one run and wrong on the next.
--
-- EXPECTED SHAPE
--   Roughly 5 to 20 rows, oldest first. One row per event, with
--   event_linkage telling you whether it came from the dispute stream or the
--   card-action stream, and minutes_since_dispute_opened giving the elapsed
--   time so the agent can say "we froze the card four minutes after you
--   called".
-- =============================================================================

WITH case_bounds AS (
  SELECT
    d.dispute_id,
    d.customer_id,
    d.card_id,
    d.opened_date,
    d.resolved_date,
    TIMESTAMP_SUB(TIMESTAMP(d.opened_date, 'UTC'), INTERVAL @pre_open_hours HOUR) AS window_from_ts,
    TIMESTAMP_ADD(
      TIMESTAMP(COALESCE(d.resolved_date, @as_of_date), 'UTC'),
      INTERVAL @post_close_hours HOUR
    ) AS window_to_ts
  FROM `{dataset}.disputes` AS d
  WHERE d.dispute_id = @dispute_id
)
SELECT
  e.event_id,
  b.dispute_id AS case_dispute_id,
  e.dispute_id AS event_dispute_id,
  e.customer_id,
  e.card_id,
  e.event_timestamp,
  e.event_type,
  e.actor_type,
  e.actor_id,
  e.channel,
  e.event_detail,
  e.shipment_tracking_id,
  e.shipment_destination_type,
  e.shipment_destination_country,
  IF(e.dispute_id = b.dispute_id, 'DISPUTE', 'CARD_ACTION') AS event_linkage,
  TIMESTAMP_DIFF(e.event_timestamp, TIMESTAMP(b.opened_date, 'UTC'), MINUTE)
    AS minutes_since_dispute_opened,
  ROW_NUMBER() OVER (ORDER BY e.event_timestamp ASC, e.event_id ASC) AS step_number
FROM `{dataset}.dispute_events` AS e
CROSS JOIN case_bounds AS b
WHERE
  e.dispute_id = b.dispute_id
  OR (
    e.dispute_id IS NULL
    AND e.card_id = b.card_id
    AND e.event_timestamp >= b.window_from_ts
    AND e.event_timestamp <= b.window_to_ts
  )
ORDER BY e.event_timestamp ASC, e.event_id ASC;
