"""India Post status synchronisation.

Polls the bulk tracking endpoint, maps scans onto the existing
``logistics.shipment`` lifecycle and writes ``logistics.shipment.event`` rows so
the public /track page keeps working unchanged.

A note on the event vocabulary. The vendor spreadsheets list codes such as
``ITEM_BOOK`` and ``BAG_DISPATCH``, but the live API returns human sentences
instead: "Item Bagged", "Item Dispatched", "Item Received", "Item Invoiced",
"Item Delivered to vinesh", "Missent - Redirected to Mangaluru H.O",
"Unclaimed", "Kept in Deposit", "Returns Confirmed". The mapper below handles
both, exact codes first and then phrase matching, so it keeps working whichever
form production sends.

Inbound webhooks at ``/indiapost/bookingeventwebhook`` and
``/indiapost/othereventwebhook`` normalise whatever payload India Post POSTs
into the same ``tracking_details`` shape and hand it to
``_ip_apply_tracking``, so polling and push share one status mapping.
"""

from odoo import api, fields, models, _

import datetime
import logging
import re

from . import indiapost_common as ipc
from .indiapost_client import IndiapostApiError

_logger = logging.getLogger(__name__)

# Logged once per (kind, key-tuple) so the first live payload can be tightened
# without flooding the log on every India Post retry.
_LOGGED_WEBHOOK_SHAPES = set()

TRACKING_PATH = '/v1/tracking/bulk'
# The endpoint accepts up to 500 barcodes per call.
TRACKING_BATCH_SIZE = 500
# Shipments in these states will never move again, so stop polling them.
TRACKING_FINAL_STATES = ('delivered', 'cancelled', 'returned')

# (shipment state, custody event type) for each recognised scan. A state of
# None means "record the scan but leave the status alone" — on-hold and
# redirection notices say nothing about progress.
_DELIVERED = ('delivered', 'delivered')
_RETURNED = ('returned', 'returned')
_OUT_FOR_DELIVERY = ('out_for_delivery', 'out_for_delivery')
_TRANSIT = ('in_transit', 'indiapost_transit_scan')
_AT_DESTINATION = ('at_destination_hub', 'indiapost_transit_scan')
_BOOKED = ('in_transit', 'indiapost_booked')
_PICKED = ('picked', 'pickup_scan')
_PICKUP_SCHEDULED = ('pickup_requested', 'indiapost_pickup_scheduled')
_CANCELLED = ('cancelled', 'indiapost_transit_scan')
_HOLD = (None, 'indiapost_transit_scan')

# Exact codes from the vendor spreadsheets. Never seen live, kept because
# production may well send them.
EVENT_CODE_MAP = {
    'UNASSIGNED': (_PICKUP_SCHEDULED, 'Pickup Request Raised'),
    'ASSIGNED': (_PICKUP_SCHEDULED, 'Pickup Assigned'),
    'CANCELLED': (_CANCELLED, 'Item Pickup Cancelled'),
    'PICKEDUP': (_PICKED, 'Item Pickedup'),
    'INDUCTED': (_BOOKED, 'Item inducted'),
    'ITEM_BOOK': (_BOOKED, 'Item Booked'),
    'BAG_DISPATCH': (_TRANSIT, 'Bag Dispatch'),
    'BAG_OPEN': (_AT_DESTINATION, 'Item Received'),
    'ITEM_INVOICE': (_OUT_FOR_DELIVERY, 'Out for delivery'),
    'ITEM_ONHOLD': (_HOLD, 'Item Kept on Hold'),
    'ITEM_REDIRECT': (_HOLD, 'Item Redirected'),
    'ITEM_RETURN': (_RETURNED, 'Item Returned to Sender'),
    'ITEM_DELIVERY': (_DELIVERED, 'Item Delivered'),
    'ACCEPTED': (_BOOKED, 'Booking Accepted'),
    'BOOKING_ACCEPTED': (_BOOKED, 'Booking Accepted'),
    'BOOKING_REJECTED': (_CANCELLED, 'Booking Rejected'),
    'REJECTED': (_CANCELLED, 'Booking Rejected'),
    'LABEL_GENERATED': (_HOLD, 'Label Generated'),
    'LABEL': (_HOLD, 'Label Generated'),
}

# Phrase rules for the free text the API actually returns. Ordered: the first
# match wins, so the more specific phrases come first.
EVENT_PHRASE_RULES = [
    ('ITEM DELIVERED', _DELIVERED, 'Item Delivered'),
    ('DELIVERED TO', _DELIVERED, 'Item Delivered'),
    ('RETURNS CONFIRMED', _RETURNED, 'Return Confirmed'),
    ('RETURNED TO SENDER', _RETURNED, 'Item Returned to Sender'),
    ('ITEM RETURN', _RETURNED, 'Item Returned to Sender'),
    ('ITEM INVOICED', _OUT_FOR_DELIVERY, 'Out for delivery'),
    ('OUT FOR DELIVERY', _OUT_FOR_DELIVERY, 'Out for delivery'),
    ('UNCLAIMED', _HOLD, 'Unclaimed'),
    ('KEPT IN DEPOSIT', _HOLD, 'Kept in Deposit'),
    ('ON HOLD', _HOLD, 'Item Kept on Hold'),
    ('ONHOLD', _HOLD, 'Item Kept on Hold'),
    ('MISSENT', _HOLD, 'Missent, being redirected'),
    ('REDIRECT', _HOLD, 'Item Redirected'),
    ('PICKUP CANCEL', _CANCELLED, 'Item Pickup Cancelled'),
    ('ITEM BOOKED', _BOOKED, 'Item Booked'),
    ('INDUCTED', _BOOKED, 'Item inducted'),
    ('ITEM BAGGED', _TRANSIT, 'Item Bagged'),
    ('ITEM DISPATCHED', _TRANSIT, 'Item Dispatched'),
    ('BAG DISPATCH', _TRANSIT, 'Bag Dispatch'),
    ('RECEIVED AT DESTINATION', _AT_DESTINATION, 'Item received at Destination'),
    ('ITEM RECEIVED', _TRANSIT, 'Item Received'),
    ('BAG OPEN', _TRANSIT, 'Item Received'),
    ('PICKEDUP', _PICKED, 'Item Pickedup'),
    ('PICKED UP', _PICKED, 'Item Pickedup'),
    ('PICKUP ASSIGNED', _PICKUP_SCHEDULED, 'Pickup Assigned'),
    ('PICKUP REQUEST', _PICKUP_SCHEDULED, 'Pickup Request Raised'),
    ('BOOKING REJECTED', _CANCELLED, 'Booking Rejected'),
    ('BOOKING ACCEPTED', _BOOKED, 'Booking Accepted'),
    ('LABEL GENERATED', _HOLD, 'Label Generated'),
]

# How far along the journey each state sits, so a late scan can never drag a
# shipment backwards.
_STATE_PROGRESS = {
    'order_added': 0,
    'pickup_requested': 1,
    'picked': 2,
    'in_transit': 3,
    'at_source_hub': 3,
    'at_central_hub': 3,
    'at_destination_hub': 4,
    'out_for_delivery': 5,
    'delivery_failed': 5,
    'delivered': 9,
    'returned': 9,
    'cancelled': 9,
}


def classify_event(raw_event):
    """Map an India Post scan onto ``(state, event_type, label)``.

    ``state`` may be None, meaning "log the scan, do not change the status".
    Unrecognised scans fall through to a neutral transit note rather than being
    dropped, so nothing is silently lost.
    """
    text = re.sub(r'\s+', ' ', str(raw_event or '')).strip()
    if not text:
        return None, 'note', ''

    key = text.upper()
    mapped = EVENT_CODE_MAP.get(key) or EVENT_CODE_MAP.get(
        key.replace(' ', '_'))
    if mapped:
        (state, event_type), label = mapped
        return state, event_type, label

    normalised = re.sub(r'[^A-Z0-9 ]+', ' ', key)
    normalised = re.sub(r'\s+', ' ', normalised).strip()
    for phrase, (state, event_type), label in EVENT_PHRASE_RULES:
        if phrase in normalised:
            return state, event_type, label
    return None, 'indiapost_transit_scan', text


# Keys observed or reasonably expected on booking_details / article payloads.
# Live bulk tracking currently returns tariff (often 0) and no dimensions;
# extra names are kept so a later API revision is stored instead of ignored.
_SCAN_WEIGHT_G_KEYS = (
    'physical_weight', 'physicalWeight', 'actual_weight', 'actualWeight',
    'charged_weight', 'chargedWeight', 'weight_g', 'article_weight',
    'reweigh_weight', 'revised_weight', 'revisedWeight',
)
_SCAN_WEIGHT_KG_KEYS = (
    'weight_kg', 'actual_weight_kg', 'physical_weight_kg',
)
_SCAN_VOL_KEYS = (
    'volumetric_weight', 'volumetricWeight', 'volume_weight',
    'volumetric_weight_g',
)
_SCAN_LENGTH_KEYS = (
    'article_length', 'articleLength', 'length_cm', 'length',
)
_SCAN_BREADTH_KEYS = (
    'article_breadth', 'articleBreadth', 'breadth_diameter', 'breadth_cm',
    'breadth', 'width', 'article_width',
)
_SCAN_HEIGHT_KEYS = (
    'article_height', 'articleHeight', 'height_cm', 'height',
)
_SCAN_TARIFF_KEYS = (
    'tariff', 'calculated_tariff', 'calculatedTariff', 'charged_amount',
    'billed_amount', 'final_amount', 'total_amount',
)
_SCAN_CHARGEABLE_KEYS = (
    'charged_weight', 'chargedWeight', 'chargeable_weight',
    'chargeable_weight_g', 'billed_weight',
)


def _ip_positive_number(value):
    if value in (None, '', False):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _ip_positive_int(value):
    number = _ip_positive_number(value)
    if number is None:
        return 0
    return int(round(number))


def _ip_first_in(node, keys):
    if not isinstance(node, dict):
        return None
    for key in keys:
        if key in node and node.get(key) not in (None, '', False):
            return node.get(key)
    return None


class IndiapostTracking(models.AbstractModel):
    _name = 'logistics.indiapost.tracking'
    _description = 'India Post Tracking Service'

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------
    @api.model
    def _ip_trackable_domain(self):
        return [
            ('fulfilment_method', '=', 'indiapost'),
            ('indiapost_article_number', '!=', False),
            ('state', 'not in', TRACKING_FINAL_STATES),
        ]

    @api.model
    def cron_sync_tracking(self, limit=2000):
        """Poll India Post for every open article and apply the scans."""
        # Catch pickup_requested orders whose shipments are already past
        # pickup (OFD / in transit / delivered) even if this poll finds
        # no new scans — stored compute is not rerun on upgrade.
        self.env['logistics.order'].sudo()._advance_stuck_pickup_requested_orders()
        Client = self.env['logistics.indiapost.client']
        if not Client._ip_is_configured():
            _logger.info('India Post tracking sync skipped: not configured.')
            return 0
        shipments = self.env['logistics.shipment'].sudo().search(
            self._ip_trackable_domain(),
            order='indiapost_last_tracking_sync asc nulls first, id',
            limit=limit,
        )
        updated = self.sync_shipments(shipments)
        # Delivered articles leave the poller; finish a pending re-quote if
        # actuals were stored earlier (typically by a webhook).
        self._ip_apply_pending_scan_adjustments(limit=limit)
        return updated

    @api.model
    def sync_shipments(self, shipments):
        """Refresh tracking for an explicit set of shipments."""
        # Polling and Sync Now may re-quote from actuals; webhooks do not.
        self = self.with_context(
            ip_allow_tariff_http=True, ip_trust_booking_tariff=True)
        shipments = shipments.filtered(lambda s: s.indiapost_article_number)
        if not shipments:
            return 0
        # Unstick pickup_requested from local shipment state before HTTP.
        shipments.mapped('order_id')._advance_picked_up_from_shipments()
        settings = self.env['logistics.indiapost.client']._ip_require_configured()
        updated = 0
        for start in range(0, len(shipments), TRACKING_BATCH_SIZE):
            batch = shipments[start:start + TRACKING_BATCH_SIZE]
            try:
                updated += self._ip_sync_batch(batch, settings)
            except IndiapostApiError as exc:
                _logger.warning(
                    'India Post tracking batch of %s failed: %s',
                    len(batch), exc.message,
                )
        # Local shipment state is enough to unstick pickup_requested even
        # when the tracking HTTP call failed or returned no new scans.
        shipments.mapped('order_id')._advance_picked_up_from_shipments()
        return updated

    @api.model
    def _ip_sync_batch(self, shipments, settings):
        barcodes = [shipment.indiapost_article_number for shipment in shipments]
        if not barcodes:
            # An empty input list comes back as data: null, so never send one.
            return 0
        response = self.env['logistics.indiapost.client'].call(
            'POST', TRACKING_PATH, body={'bulk': barcodes},
            operation='tracking', settings=settings,
        )
        records = response.payload.get('data') \
            if isinstance(response.payload, dict) else response.payload
        by_barcode = {
            shipment.indiapost_article_number: shipment for shipment in shipments
        }
        now = fields.Datetime.now()
        updated = 0
        for record in (records or []):
            if not isinstance(record, dict):
                continue
            booking = record.get('booking_details') or {}
            barcode = booking.get('article_number')
            shipment = by_barcode.get(barcode)
            if not shipment:
                continue
            if self._ip_apply_tracking(shipment, record):
                updated += 1
        # Stamp the whole batch, including articles with no scans yet, so the
        # cron rotates through everything instead of retrying the same records.
        shipments.sudo().write({'indiapost_last_tracking_sync': now})
        return updated

    # ------------------------------------------------------------------
    # Applying scans
    # ------------------------------------------------------------------
    @api.model
    def _ip_apply_tracking(self, shipment, record):
        """Write one article's scans onto its shipment. True if anything moved."""
        del_status = record.get('del_status')
        if isinstance(del_status, dict):
            del_status = del_status.get('del_status')
        scans = [scan for scan in (record.get('tracking_details') or [])
                 if isinstance(scan, dict)]
        extracted = self._ip_extract_scan_actuals(record)

        # An unbooked barcode returns success with no scans and
        # del_status "not delivered", which is indistinguishable from a booked
        # article awaiting its first scan. Neither tells us anything — unless
        # booking_details now carries a reweigh / tariff we can persist.
        if not scans:
            if del_status and del_status != shipment.indiapost_del_status:
                shipment.sudo().write({'indiapost_del_status': del_status})
            self._ip_try_scan_adjustment(shipment, extracted)
            shipment.order_id._advance_picked_up_from_shipments()
            return False

        parsed = self._ip_parse_scans(scans)
        created = self._ip_create_events(shipment, parsed)
        target = self._ip_target_state(shipment, parsed)
        vals = {}
        if del_status and del_status != shipment.indiapost_del_status:
            vals['indiapost_del_status'] = del_status
        if target and target != shipment.state:
            vals['state'] = target
            vals.update(self._ip_state_timestamps(shipment, target, parsed))
        if vals:
            shipment.sudo()._write_with_state(vals)
        self._ip_try_scan_adjustment(shipment, extracted)
        # Re-applying OFD does not rewrite shipment.state, so force the
        # parent order compute for already-out-for-delivery articles.
        shipment.order_id._advance_picked_up_from_shipments()
        return bool(created or vals)

    @api.model
    def _ip_extract_scan_actuals(self, record):
        """Pull weight / dims / tariff from a tracking or webhook payload.

        Live bulk tracking (CEPT proof, 2026-09-06) returns booking_details
        with article_number, booked_at, booked_on, origin_pincode,
        destination_pincode, tariff (0 on the scanned sample), article_type,
        delivery_location, delivery_confirmed_on — and tracking_details scans
        of {date, time, office, officeid, event}. No physical_weight,
        volumetric_weight, dimensions, or billed amount. Missing keys stay
        absent; we never invent them.
        """
        if not isinstance(record, dict):
            return {}
        nodes = [record]
        for key in ('booking_details', 'bookingDetails', 'article', 'data',
                    'payload'):
            nested = record.get(key)
            if isinstance(nested, dict):
                nodes.append(nested)
        found = {}
        raw = {}
        for node in nodes:
            weight = _ip_first_in(node, _SCAN_WEIGHT_G_KEYS)
            if weight is not None and not found.get('weight_g'):
                grams = _ip_positive_int(weight)
                if grams:
                    found['weight_g'] = grams
                    raw['weight_g'] = weight
            weight_kg = _ip_first_in(node, _SCAN_WEIGHT_KG_KEYS)
            if weight_kg is not None and not found.get('weight_g'):
                grams = ipc.kg_to_grams(weight_kg)
                if grams:
                    found['weight_g'] = grams
                    raw['weight_kg'] = weight_kg
            vol = _ip_first_in(node, _SCAN_VOL_KEYS)
            if vol is not None and not found.get('volumetric_g'):
                grams = _ip_positive_int(vol)
                if grams:
                    found['volumetric_g'] = grams
                    raw['volumetric_g'] = vol
            length = _ip_first_in(node, _SCAN_LENGTH_KEYS)
            if length is not None and not found.get('length_cm'):
                cms = _ip_positive_int(length)
                if cms:
                    found['length_cm'] = cms
                    raw['length_cm'] = length
            breadth = _ip_first_in(node, _SCAN_BREADTH_KEYS)
            if breadth is not None and not found.get('breadth_cm'):
                cms = _ip_positive_int(breadth)
                if cms:
                    found['breadth_cm'] = cms
                    raw['breadth_cm'] = breadth
            height = _ip_first_in(node, _SCAN_HEIGHT_KEYS)
            if height is not None and not found.get('height_cm'):
                cms = _ip_positive_int(height)
                if cms:
                    found['height_cm'] = cms
                    raw['height_cm'] = height
            chargeable = _ip_first_in(node, _SCAN_CHARGEABLE_KEYS)
            if chargeable is not None and not found.get('chargeable_g'):
                grams = _ip_positive_int(chargeable)
                if grams:
                    found['chargeable_g'] = grams
                    raw['chargeable_g'] = chargeable
            tariff = _ip_first_in(node, _SCAN_TARIFF_KEYS)
            if tariff is not None and not found.get('tariff'):
                amount = _ip_positive_number(tariff)
                if amount:
                    found['tariff'] = amount
                    raw['tariff'] = tariff
        if raw:
            found['raw'] = raw
        return found

    @api.model
    def _ip_try_scan_adjustment(self, shipment, extracted):
        """Apply a volumetric / quote adjustment without failing tracking."""
        if not shipment:
            return
        try:
            shipment._ip_apply_scan_rate_adjustment(
                extracted=dict(
                    extracted or {},
                    tariff_trusted=bool(
                        self.env.context.get('ip_trust_booking_tariff')),
                ),
                allow_tariff_http=bool(
                    self.env.context.get('ip_allow_tariff_http')),
                allow_api_tariff=bool(
                    self.env.context.get('ip_trust_booking_tariff')),
            )
        except Exception:
            _logger.exception(
                'India Post scan adjustment after tracking failed for %s',
                shipment.name,
            )

    @api.model
    def _ip_apply_pending_scan_adjustments(self, limit=500):
        """Finish re-quotes that a webhook stored without calling tariff HTTP."""
        shipments = self.env['logistics.shipment'].sudo().search([
            ('fulfilment_method', '=', 'indiapost'),
            ('indiapost_scan_quote_pending', '=', True),
            ('indiapost_scan_adjusted', '=', False),
            ('is_return_journey', '=', False),
            ('wallet_transaction_id', '!=', False),
        ], limit=limit, order='id')
        if shipments:
            shipments.with_context(
                ip_allow_tariff_http=True, ip_trust_booking_tariff=True,
            )._ip_apply_scan_rate_adjustment(
                allow_tariff_http=True, allow_api_tariff=True)
        return len(shipments)

    @api.model
    def _ip_parse_scans(self, scans):
        """Normalise scan dicts into the shape ``_ip_create_events`` expects."""
        parsed = []
        for scan in scans:
            if not isinstance(scan, dict):
                continue
            moment = self._ip_scan_datetime(scan)
            state, event_type, label = classify_event(scan.get('event'))
            parsed.append({
                'moment': moment,
                'state': state,
                'event_type': event_type,
                'label': label,
                'office': (scan.get('office') or '').strip(),
                'office_id': str(scan.get('officeid') or ''),
                'raw': re.sub(r'\s+', ' ', str(scan.get('event') or '')).strip(),
                'event_id': str(
                    scan.get('eventId') or scan.get('event_id')
                    or scan.get('eventID') or ''
                ).strip(),
            })
        parsed.sort(key=lambda item: (item['moment'] or datetime.datetime.min,
                                      item['raw']))
        return parsed

    @api.model
    def _ip_scan_datetime(self, scan):
        """Combine the scan's date and time, then store naive UTC.

        Bulk tracking ``date`` looks like 2026-02-19T00:00:00Z and carries no
        useful time; the real clock time is in ``time`` as HH:MM:SS. Webhook
        payloads may send a single ISO datetime instead. Naive clocks are
        Asia/Kolkata; timezone-aware values keep their own offset.
        """
        for key in ('eventDateTime', 'event_date_time', 'datetime',
                    'timestamp', 'dateTime'):
            parsed = self._ip_parse_iso_datetime(scan.get(key))
            if parsed:
                return ipc.to_odoo_utc(parsed)
        raw_date = str(scan.get('date') or scan.get('eventDate')
                       or scan.get('event_date') or '')
        raw_time = str(scan.get('time') or scan.get('eventTime')
                       or scan.get('event_time') or '').strip()
        if 'T' in raw_date and not raw_time:
            parsed = self._ip_parse_iso_datetime(raw_date)
            if parsed:
                return ipc.to_odoo_utc(parsed)
        day = self._ip_parse_scan_date(raw_date)
        if day is None:
            return ipc.to_odoo_utc(self._ip_parse_iso_datetime(raw_date))
        clock = datetime.time()
        for fmt in ('%H:%M:%S', '%H:%M'):
            try:
                clock = datetime.datetime.strptime(raw_time, fmt).time()
                break
            except ValueError:
                continue
        return ipc.to_odoo_utc(datetime.datetime.combine(day, clock))

    @staticmethod
    def _ip_parse_scan_date(raw_date):
        """Calendar date from bulk ``date`` or webhook ``eventDate``."""
        text = str(raw_date or '').strip()
        if not text:
            return None
        head = text[:10]
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
            try:
                return datetime.datetime.strptime(head, fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _ip_parse_iso_datetime(raw):
        """Parse an India Post datetime, keeping tzinfo when the string has one."""
        text = str(raw or '').strip()
        if not text:
            return None
        text = text.replace('Z', '+00:00')
        try:
            return datetime.datetime.fromisoformat(text)
        except ValueError:
            pass
        for fmt, size in (
            ('%Y-%m-%d %H:%M:%S', 19),
            ('%Y-%m-%d %H:%M', 16),
            ('%d-%m-%Y %H:%M:%S', 19),
            ('%d-%m-%Y %H:%M', 16),
            ('%d/%m/%Y %H:%M:%S', 19),
            ('%d/%m/%Y %H:%M', 16),
            ('%Y-%m-%d', 10),
            ('%d-%m-%Y', 10),
            ('%d/%m/%Y', 10),
        ):
            try:
                return datetime.datetime.strptime(text[:size], fmt)
            except ValueError:
                continue
        return None

    @api.model
    def _ip_create_events(self, shipment, parsed):
        """Create custody events for scans we have not seen before."""
        Event = self.env['logistics.shipment.event'].sudo()
        known = set(Event.search([
            ('shipment_id', '=', shipment.id),
            ('indiapost_event_key', '!=', False),
        ]).mapped('indiapost_event_key'))
        created = 0
        for item in parsed:
            key = self._ip_event_key(item)
            if key in known:
                continue
            known.add(key)
            note = item['label'] or item['raw']
            if item['raw'] and item['label'] and item['raw'] != item['label']:
                note = '%s (%s)' % (item['label'], item['raw'])
            try:
                with self.env.cr.savepoint():
                    Event.create({
                        'shipment_id': shipment.id,
                        'event_type': item['event_type'],
                        'event_time': item['moment'] or fields.Datetime.now(),
                        'actor_user_id': False,
                        'note': note,
                        'indiapost_event_code': item['raw'][:120],
                        'indiapost_event_key': key,
                        'indiapost_office_name': item['office'][:120],
                    })
            except Exception as exc:
                # Duplicate article+code+timestamp (or event id) from a
                # webhook retry. The unique constraint is the last line of
                # defence after the in-memory ``known`` set.
                message = str(exc)
                if 'indiapost_scan_uniq' in message \
                        or 'already been recorded' in message:
                    continue
                raise
            created += 1
        return created

    @staticmethod
    def _ip_event_key(item):
        """Stable identity for a scan, so repeated polls do not duplicate it.

        The timestamp in the key is the India Post IST wall clock, not the
        UTC storage value. Older events were keyed from naive IST stored as
        if it were UTC; using that same clock means a re-poll after the
        timezone fix will not duplicate them. Those public history rows stay
        shifted until a genuinely new scan arrives.
        """
        event_id = str(item.get('event_id') or '').strip()
        if event_id:
            return ('id|%s' % event_id)[:255]
        moment = item['moment']
        if moment:
            wall = ipc.odoo_utc_as_ist(moment)
            stamp = wall.strftime('%Y%m%d%H%M%S')
        else:
            stamp = '-'
        return '%s|%s|%s' % (stamp, item['office_id'] or item['office'],
                             item['raw'])[:255]

    @api.model
    def _ip_target_state(self, shipment, parsed):
        """The state the scans imply, or None to leave the shipment alone."""
        if shipment.state in TRACKING_FINAL_STATES:
            return None

        # "Item Delivered" after a return has been confirmed means delivered
        # back to the sender, which for us is a return, not a delivery. The
        # vendor's own note on ITEM_DELIVERY says "addressee or sender".
        return_seen = False
        target = None
        for item in parsed:
            state = item['state']
            if state == 'returned':
                return_seen = True
            if not state:
                continue
            if state == 'delivered' and return_seen:
                state = 'returned'
            target = state

        if not target or target == shipment.state:
            return None
        # Never regress: a late in-transit scan must not pull an out-for-delivery
        # shipment backwards.
        if _STATE_PROGRESS.get(target, 0) < _STATE_PROGRESS.get(shipment.state, 0):
            return None
        return target

    @api.model
    def _ip_state_timestamps(self, shipment, target, parsed):
        """Fill the lifecycle timestamps the rest of the module reads."""
        vals = {}
        moments = {}
        for item in parsed:
            if item['state']:
                moments.setdefault(item['state'], item['moment'])
        if target == 'picked' and not shipment.picked_on:
            vals['picked_on'] = moments.get('picked') or fields.Datetime.now()
        if target == 'delivered':
            moment = moments.get('delivered') or fields.Datetime.now()
            if not shipment.delivered_on:
                vals['delivered_on'] = moment
            if not shipment.actual_delivery_date:
                vals['actual_delivery_date'] = moment
            vals['custodian_type'] = 'customer'
        if target == 'returned':
            vals['custodian_type'] = 'seller'
        return vals

    # ------------------------------------------------------------------
    # Manual refresh
    # ------------------------------------------------------------------
    @api.model
    def action_ip_sync_now(self, shipments):
        updated = self.sync_shipments(shipments)
        self._ip_apply_pending_scan_adjustments(limit=max(len(shipments), 1))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('India Post tracking'),
                'message': _('%(updated)s of %(total)s shipment(s) had new '
                             'scans.') % {'updated': updated,
                                          'total': len(shipments)},
                'type': 'success',
                'sticky': False,
            },
        }

    # ------------------------------------------------------------------
    # Inbound webhooks
    # ------------------------------------------------------------------
    # India Post has not published the POST body. Walk common article / event
    # keys (and nested ``data``) so a Test ping and a real scan both land.
    _WEBHOOK_ARTICLE_KEYS = frozenset({
        'articlenumber', 'article_number', 'articleno', 'article_no',
        'barcodeno', 'barcode_no', 'barcode', 'awb', 'awbnumber',
        'awb_number', 'articleid', 'article_id',
    })
    _WEBHOOK_EVENT_KEYS = (
        'event', 'eventCode', 'event_code', 'eventName', 'event_name',
        'eventType', 'event_type', 'status', 'statusCode', 'status_code',
        'eventDescription', 'event_description', 'event_desc', 'remarks',
        'message',
    )
    _WEBHOOK_EVENT_ID_KEYS = (
        'eventId', 'event_id', 'eventID', 'txnId', 'txn_id', 'messageId',
        'message_id',
    )
    _WEBHOOK_OFFICE_KEYS = (
        'office', 'officeName', 'office_name', 'location',
    )
    _WEBHOOK_OFFICE_ID_KEYS = (
        'officeid', 'officeId', 'office_id', 'officeCode', 'office_code',
    )
    _WEBHOOK_SCAN_LIST_KEYS = (
        'tracking_details', 'trackingDetails', 'events', 'eventList',
        'event_list', 'scans', 'statusHistory', 'status_history',
    )

    @staticmethod
    def _ip_norm_key(key):
        return re.sub(r'[^a-z0-9]', '', str(key).lower())

    @classmethod
    def _ip_first_str(cls, node, keys):
        if not isinstance(node, dict):
            return ''
        for key in keys:
            value = node.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
        return ''

    @api.model
    def _ip_log_webhook_keys(self, kind, payload):
        if isinstance(payload, dict):
            keys = tuple(sorted(str(key) for key in payload.keys()))
        elif isinstance(payload, list):
            keys = ('<list:%s>' % len(payload),)
        else:
            keys = (type(payload).__name__,)
        shape = (kind, keys)
        if shape in _LOGGED_WEBHOOK_SHAPES:
            return
        _LOGGED_WEBHOOK_SHAPES.add(shape)
        _logger.info('India Post %s webhook payload keys: %s', kind, list(keys))

    @api.model
    def _ip_article_from(self, node, depth=0):
        """First article / barcode / AWB found while walking ``node``."""
        if depth > 6 or node is None:
            return ''
        if isinstance(node, dict):
            for key, value in node.items():
                if self._ip_norm_key(key) in self._WEBHOOK_ARTICLE_KEYS:
                    if isinstance(value, (str, int)) and str(value).strip():
                        return str(value).strip()
            nested = node.get('booking_details') or node.get('bookingDetails')
            found = self._ip_article_from(nested, depth + 1)
            if found:
                return found
            for key in ('data', 'payload', 'result', 'body', 'event', 'record'):
                found = self._ip_article_from(node.get(key), depth + 1)
                if found:
                    return found
            return ''
        if isinstance(node, (list, tuple)):
            for item in node[:50]:
                found = self._ip_article_from(item, depth + 1)
                if found:
                    return found
        return ''

    @api.model
    def _ip_normalize_scan(self, item):
        if isinstance(item, str):
            return {
                'event': item, 'date': '', 'time': '',
                'office': '', 'officeid': '', 'event_id': '',
            }
        if not isinstance(item, dict):
            return {}
        event = self._ip_first_str(item, self._WEBHOOK_EVENT_KEYS)
        office = self._ip_first_str(item, self._WEBHOOK_OFFICE_KEYS)
        officeid = self._ip_first_str(item, self._WEBHOOK_OFFICE_ID_KEYS)
        event_id = self._ip_first_str(item, self._WEBHOOK_EVENT_ID_KEYS)
        date = item.get('date') or item.get('eventDate') or item.get('event_date') or ''
        time = item.get('time') or item.get('eventTime') or item.get('event_time') or ''
        combined = self._ip_first_str(item, (
            'eventDateTime', 'event_date_time', 'dateTime', 'datetime',
            'timestamp',
        ))
        if not combined:
            maybe_date = str(
                item.get('eventDate') or item.get('event_date') or '')
            if 'T' in maybe_date or (
                    len(maybe_date.strip()) > 10 and ' ' in maybe_date):
                combined = maybe_date.strip()
        scan = {
            'event': event,
            'date': date,
            'time': time,
            'office': office,
            'officeid': officeid,
            'event_id': event_id,
        }
        if combined:
            # Keep the original string so ``_ip_scan_datetime`` can see a UTC
            # offset and avoid treating an aware timestamp as naive IST.
            scan['eventDateTime'] = combined
        return scan

    @api.model
    def _ip_scans_from(self, payload):
        if not isinstance(payload, dict):
            return []
        for key in self._WEBHOOK_SCAN_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list) and value:
                scans = []
                for item in value:
                    if isinstance(item, str):
                        scans.append(self._ip_normalize_scan(item))
                    elif isinstance(item, dict):
                        scans.append(self._ip_normalize_scan(item))
                return [scan for scan in scans if scan.get('event') or scan.get('event_id')]
        nested = payload.get('data')
        if isinstance(nested, dict):
            nested_scans = self._ip_scans_from(nested)
            if nested_scans:
                return nested_scans
        scan = self._ip_normalize_scan(payload)
        if scan.get('event') or scan.get('event_id'):
            return [scan]
        return []

    @api.model
    def _ip_coerce_webhook_records(self, payload):
        """Turn an arbitrary webhook body into bulk-tracking-shaped records."""
        if payload in (None, '', False, {}, []):
            return []
        if isinstance(payload, list):
            records = []
            for item in payload:
                records.extend(self._ip_coerce_webhook_records(item))
            return records
        if not isinstance(payload, dict):
            return []

        article = self._ip_article_from(payload)
        scans = self._ip_scans_from(payload)
        del_status = payload.get('del_status')
        if isinstance(del_status, dict):
            del_status = del_status.get('del_status')

        if not article and not scans:
            nested = payload.get('data') or payload.get('payload') \
                or payload.get('result') or payload.get('body')
            if isinstance(nested, (dict, list)):
                return self._ip_coerce_webhook_records(nested)
            return []

        return [{
            'article': article,
            'del_status': del_status,
            'tracking_details': scans,
            'booking_details': (
                payload.get('booking_details')
                or payload.get('bookingDetails') or {}
            ),
            # Keep original keys so extract can see a future weight / tariff
            # without treating them as a delivery-charge write.
            'payload': payload,
        }]

    @api.model
    def _ip_find_shipment(self, article):
        article = (article or '').strip()
        if not article:
            return self.env['logistics.shipment']
        Shipment = self.env['logistics.shipment'].sudo()
        shipment = Shipment.search(
            [('indiapost_article_number', '=', article)], limit=1)
        if shipment:
            return shipment
        shipment = Shipment.search([('name', '=', article)], limit=1)
        if shipment:
            return shipment
        barcode = self.env['logistics.indiapost.barcode'].sudo().search(
            [('barcode', '=', article)], limit=1)
        return barcode.shipment_id

    @api.model
    def _ip_apply_booking_side_effects(self, shipment, parsed):
        """Record booked / rejected without touching tariff or charges."""
        if not parsed:
            return
        if shipment.indiapost_booking_state == 'booked':
            return
        for item in parsed:
            if item['event_type'] == 'indiapost_booked':
                vals = {'indiapost_booking_state': 'booked'}
                if not shipment.indiapost_booked_on:
                    vals['indiapost_booked_on'] = (
                        item['moment'] or fields.Datetime.now())
                shipment.sudo().write(vals)
                return
            if item['state'] == 'cancelled':
                shipment.sudo().write({
                    'indiapost_booking_state': 'error',
                    'indiapost_booking_error': (
                        item['raw'] or item['label'] or 'Booking rejected'),
                })
                return

    @api.model
    def ingest_webhook(self, kind, payload):
        """Apply one inbound webhook. Never raises; caller logs and returns 200.

        Charge keys in the payload never rewrite ``delivery_charges_total``.
        Weight / dimension / tariff figures are stored for a later scan
        adjustment; the wallet line is posted only from tracking poll / cron
        (tariff re-quote) or the ops button, not from this HTTP request.
        """
        self._ip_log_webhook_keys(kind, payload)
        settings = self.env['logistics.indiapost.client']._ip_settings()
        if not settings.get('indiapost_webhooks_enabled', True):
            _logger.info('India Post %s webhook ignored: acceptance is off.', kind)
            return

        records = self._ip_coerce_webhook_records(payload)
        for record in records:
            article = record.get('article')
            if not article:
                continue
            shipment = self._ip_find_shipment(article)
            if not shipment:
                _logger.info(
                    'India Post %s webhook: unknown article %s', kind, article)
                continue
            tracking_record = {
                'del_status': record.get('del_status'),
                'tracking_details': record.get('tracking_details') or [],
                'booking_details': record.get('booking_details') or {},
                'payload': record.get('payload') or {},
            }
            self._ip_apply_tracking(shipment, tracking_record)
            if kind == 'booking':
                self._ip_apply_booking_side_effects(
                    shipment, self._ip_parse_scans(
                        tracking_record['tracking_details']))

