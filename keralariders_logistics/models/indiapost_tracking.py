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
        Client = self.env['logistics.indiapost.client']
        if not Client._ip_is_configured():
            _logger.info('India Post tracking sync skipped: not configured.')
            return 0
        shipments = self.env['logistics.shipment'].sudo().search(
            self._ip_trackable_domain(),
            order='indiapost_last_tracking_sync asc nulls first, id',
            limit=limit,
        )
        return self.sync_shipments(shipments)

    @api.model
    def sync_shipments(self, shipments):
        """Refresh tracking for an explicit set of shipments."""
        shipments = shipments.filtered(lambda s: s.indiapost_article_number)
        if not shipments:
            return 0
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

        # An unbooked barcode returns success with no scans and
        # del_status "not delivered", which is indistinguishable from a booked
        # article awaiting its first scan. Neither tells us anything.
        if not scans:
            if del_status and del_status != shipment.indiapost_del_status:
                shipment.sudo().write({'indiapost_del_status': del_status})
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
        return bool(created or vals)

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
        """Combine the scan's ISO date with its separate time field.

        Bulk tracking ``date`` looks like 2026-02-19T00:00:00Z and carries no
        useful time; the real clock time is in ``time`` as HH:MM:SS. Webhook
        payloads may send a single ISO datetime instead.
        """
        for key in ('eventDateTime', 'event_date_time', 'datetime',
                    'timestamp', 'dateTime'):
            parsed = self._ip_parse_iso_datetime(scan.get(key))
            if parsed:
                return parsed
        raw_date = str(scan.get('date') or scan.get('eventDate')
                       or scan.get('event_date') or '')
        raw_time = str(scan.get('time') or scan.get('eventTime')
                       or scan.get('event_time') or '').strip()
        if 'T' in raw_date and not raw_time:
            parsed = self._ip_parse_iso_datetime(raw_date)
            if parsed:
                return parsed
        try:
            day = datetime.datetime.strptime(raw_date[:10], '%Y-%m-%d').date()
        except ValueError:
            return self._ip_parse_iso_datetime(raw_date)
        for fmt in ('%H:%M:%S', '%H:%M'):
            try:
                clock = datetime.datetime.strptime(raw_time, fmt).time()
                break
            except ValueError:
                clock = datetime.time()
        return datetime.datetime.combine(day, clock)

    @staticmethod
    def _ip_parse_iso_datetime(raw):
        text = str(raw or '').strip()
        if not text:
            return None
        text = text.replace('Z', '+00:00')
        try:
            parsed = datetime.datetime.fromisoformat(text)
            return parsed.replace(tzinfo=None)
        except ValueError:
            pass
        for fmt, size in (
            ('%Y-%m-%d %H:%M:%S', 19),
            ('%Y-%m-%d %H:%M', 16),
            ('%d-%m-%Y %H:%M:%S', 19),
            ('%d/%m/%Y %H:%M:%S', 19),
            ('%Y-%m-%d', 10),
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
        """Stable identity for a scan, so repeated polls do not duplicate it."""
        event_id = str(item.get('event_id') or '').strip()
        if event_id:
            return ('id|%s' % event_id)[:255]
        moment = item['moment'].strftime('%Y%m%d%H%M%S') if item['moment'] else '-'
        return '%s|%s|%s' % (moment, item['office_id'] or item['office'],
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
        date = item.get('date') or ''
        time = item.get('time') or ''
        combined = self._ip_first_str(item, (
            'eventDateTime', 'event_date_time', 'eventDate', 'event_date',
            'dateTime', 'datetime', 'timestamp',
        ))
        if combined:
            parsed = self._ip_parse_iso_datetime(combined)
            if parsed:
                date = parsed.strftime('%Y-%m-%dT00:00:00Z')
                time = parsed.strftime('%H:%M:%S')
        return {
            'event': event,
            'date': date,
            'time': time,
            'office': office,
            'officeid': officeid,
            'event_id': event_id,
        }

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

        Tariff / charge keys in the payload are ignored on purpose: India Post
        must not be able to set what a seller is billed.
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
            }
            self._ip_apply_tracking(shipment, tracking_record)
            if kind == 'booking':
                self._ip_apply_booking_side_effects(
                    shipment, self._ip_parse_scans(
                        tracking_record['tracking_details']))

