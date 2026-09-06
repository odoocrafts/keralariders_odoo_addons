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

The inbound webhook mentioned in the vendor document has no specified
authentication, source IPs or retry policy, so this cron is the only status
source. :mod:`controllers.indiapost_webhook` holds a disabled placeholder that
can be wired up without touching anything here.
"""

from odoo import api, fields, models, _

import datetime
import logging
import re

from .indiapost_client import IndiapostApiError

_logger = logging.getLogger(__name__)

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

        parsed = []
        for scan in scans:
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
            })
        parsed.sort(key=lambda item: (item['moment'] or datetime.datetime.min,
                                      item['raw']))

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
    def _ip_scan_datetime(self, scan):
        """Combine the scan's ISO date with its separate time field.

        ``date`` looks like 2026-02-19T00:00:00Z and carries no useful time;
        the real clock time is in ``time`` as HH:MM:SS.
        """
        raw_date = str(scan.get('date') or '')[:10]
        raw_time = str(scan.get('time') or '').strip()
        try:
            day = datetime.datetime.strptime(raw_date, '%Y-%m-%d').date()
        except ValueError:
            return None
        for fmt in ('%H:%M:%S', '%H:%M'):
            try:
                clock = datetime.datetime.strptime(raw_time, fmt).time()
                break
            except ValueError:
                clock = datetime.time()
        return datetime.datetime.combine(day, clock)

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
            created += 1
        return created

    @staticmethod
    def _ip_event_key(item):
        """Stable identity for a scan, so repeated polls do not duplicate it."""
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
