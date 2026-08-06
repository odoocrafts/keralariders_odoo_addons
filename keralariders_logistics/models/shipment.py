from odoo import models, fields, api, _
from odoo.exceptions import UserError
import re
import uuid

delivery_states = [
    ('order_added', 'Order Added'),
    ('pickup_requested', 'Pickup Requested'),
    ('picked', 'Picked'),
    ('in_transit', 'In Transit'),
    ('at_source_hub', 'At Source Hub'),
    ('at_central_hub', 'At Central Hub'),
    ('at_destination_hub', 'At Destination Hub'),
    ('out_for_delivery', 'Out for Delivery'),
    ('delivered', 'Delivered'),
    ('cancelled', 'Cancelled'),
    ('return_requested', 'Return Requested'),
    ('return_picked', 'Return Picked'),
    ('returned', 'Returned'),
]

class Shipment(models.Model):
    _name = 'logistics.shipment'
    _description = 'Shipment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'name desc, write_date desc'

    def init(self):
        """Migrate legacy duplicate cancel key → cancelled."""
        self.env.cr.execute("""
            UPDATE logistics_shipment
               SET state = 'cancelled'
             WHERE state = 'cancel'
        """)

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"AWB - {rec.name}"
    
    name = fields.Char(string='Shipment Reference (AWB)', required=True, copy=False, readonly=True, index=True, default=lambda self: _('New'))
    tracking_token = fields.Char(string='Tracking Token', default=lambda self: str(uuid.uuid4()), copy=False, index=True)
    tracking_url = fields.Char(string='Tracking URL', compute='_compute_tracking_url')

    def _compute_tracking_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        for shipment in self:
            if shipment.tracking_token:
                shipment.tracking_url = f"{base_url}/track/{shipment.tracking_token}"
            else:
                shipment.tracking_url = False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].sudo().next_by_code('logistics.shipment') or _('New')
            # Initial custody: package starts with the seller until DE pickup.
            vals.setdefault('custodian_type', 'seller')
            if vals.get('state') == 'cancel':
                vals['state'] = 'cancelled'
        shipments = super(Shipment, self).create(vals_list)
        shipments.filtered(lambda s: s.estimated_route_ids and not s.active_leg_id)._sync_active_leg()
        return shipments

    def write(self, vals):
        if vals.get('state') == 'cancel':
            vals = dict(vals, state='cancelled')
        # Harden: non-admins cannot freely write state via RPC/UI; use transition methods.
        if 'state' in vals and not self.env.context.get('allow_shipment_state_write'):
            is_admin = self.env.user.has_group('keralariders_logistics.group_logistics_admin')
            if not is_admin:
                raise UserError(_(
                    "Shipment status can only be changed via the dedicated action buttons "
                    "(pickup, hub receive, dispatch, deliver, etc.). "
                    "Contact a Logistics Administrator for overrides."
                ))
        return super().write(vals)

    def _write_with_state(self, vals):
        """Internal helper for transition methods that may update state."""
        return self.with_context(allow_shipment_state_write=True).write(vals)

    # -------------------------------------------------------------------------
    # Custody
    # Custody after DE "drop at hub" stays with DE until hub manager receive
    # scan — that receive is the source of truth that sets custodian_type=hub.
    # -------------------------------------------------------------------------
    custodian_type = fields.Selection([
        ('seller', 'Seller'),
        ('de', 'Delivery Executive'),
        ('hub', 'Hub'),
        ('customer', 'Customer'),
    ], string='Custodian', default='seller', tracking=True, index=True)
    custodian_de_id = fields.Many2one(
        'logistics.delivery.executive',
        string='Custodian DE',
        tracking=True,
    )
    current_hub_id = fields.Many2one('logistics.hub', string='Current Hub', tracking=True, index=True)
    active_leg_id = fields.Many2one('logistics.shipment.estimated.route', string='Active Route Leg')
    route_locked = fields.Boolean(
        string='Route Locked',
        default=False,
        help='When set, estimated route is not recomputed (ops have started). Admins can force recompute.',
    )
    event_ids = fields.One2many('logistics.shipment.event', 'shipment_id', string='Custody Events')
    event_count = fields.Integer(compute='_compute_event_count')

    @api.depends('event_ids')
    def _compute_event_count(self):
        for rec in self:
            rec.event_count = len(rec.event_ids)

    def _lock_route(self):
        self.filtered(lambda s: not s.route_locked).write({'route_locked': True})

    def _create_custody_event(self, event_type, to_custodian=None, hub=None, actor_de=None,
                              scanned_code=None, note=None, leg=None):
        """Create a custody event for each shipment in self."""
        Event = self.env['logistics.shipment.event']
        for shipment in self:
            Event.create({
                'shipment_id': shipment.id,
                'event_type': event_type,
                'event_time': fields.Datetime.now(),
                'actor_user_id': self.env.user.id,
                'actor_de_id': actor_de.id if actor_de else False,
                'hub_id': hub.id if hub else (shipment.current_hub_id.id if shipment.current_hub_id else False),
                'leg_id': leg.id if leg else (shipment.active_leg_id.id if shipment.active_leg_id else False),
                'from_custodian_type': shipment.custodian_type,
                'to_custodian_type': to_custodian or shipment.custodian_type,
                'scanned_code': scanned_code,
                'note': note,
            })

    # -------------------------------------------------------------------------
    # Public / portal tracking helpers
    # -------------------------------------------------------------------------
    _TRACKING_PROGRESS_STEPS = [
        ('order_placed', 'Order Placed'),
        ('picked_up', 'Picked Up'),
        ('at_hub', 'At Hub'),
        ('in_transit', 'In Transit'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered'),
    ]

    # Map shipment.state → progress step index (0-based). -1 = terminal non-delivery.
    _STATE_TO_PROGRESS_INDEX = {
        'order_added': 0,
        'pickup_requested': 0,
        'picked': 1,
        'at_source_hub': 2,
        'at_central_hub': 2,
        'at_destination_hub': 2,
        'in_transit': 3,
        'out_for_delivery': 4,
        'delivered': 5,
        'cancelled': -1,
        # Return journey reuses hub progress steps after pickup
        'return_requested': 0,
        'return_picked': 1,
        'returned': 5,
    }

    is_return_journey = fields.Boolean(
        string='Return Journey',
        default=False,
        copy=False,
        tracking=True,
        help='When set, the package is on a reverse path: '
             'customer pickup → hubs → seller.',
    )

    # Customer-facing timeline labels (avoid internal ops / billing jargon).
    _PUBLIC_TIMELINE_LABELS = {
        'order_added': 'Order Placed',
        'pickup_requested': 'Pickup Requested',
        'pickup_scan': 'Picked Up',
        'dropped_at_hub': 'Dropped at Hub',
        'hub_receive': 'Received at Hub',
        'hub_dispatch': 'Dispatched from Hub',
        'depart_hub': 'Departed Hub',
        'de_self_assign': 'Assigned for Delivery',
        'leg_assign': 'Assigned',
        'central_pass_through': 'Passed via Hub',
        'skip_hub_local': 'Out for Local Delivery',
        'out_for_delivery': 'Out for Delivery',
        'delivered': 'Delivered',
        'return_requested': 'Return requested',
        'returned': 'Returned to sender',
        'cancelled': 'Cancelled',
        'status_override': 'Status Updated',
        'note': 'Update',
    }
    _PUBLIC_RETURN_TIMELINE_LABELS = {
        'pickup_scan': 'Picked up for return',
        'out_for_delivery': 'Out for delivery to sender',
        'returned': 'Returned to sender',
        'return_requested': 'Return requested',
    }
    _PUBLIC_STATUS_LABELS = {
        'order_added': 'Order Placed',
        'pickup_requested': 'Pickup Requested',
        'picked': 'Picked Up',
        'in_transit': 'In Transit',
        'at_source_hub': 'At Source Hub',
        'at_central_hub': 'At Central Hub',
        'at_destination_hub': 'At Destination Hub',
        'out_for_delivery': 'Out for Delivery',
        'delivered': 'Delivered',
        'cancelled': 'Cancelled',
        'return_requested': 'Return requested',
        'return_picked': 'Picked up for return',
        'returned': 'Returned to sender',
    }

    def _format_tracking_datetime(self, value):
        """Format a datetime (or date) for public tracking display."""
        self.ensure_one()
        if not value:
            return ''
        if isinstance(value, str):
            value = fields.Datetime.from_string(value)
        # Date-only (no time) — format without time
        if not hasattr(value, 'hour'):
            return value.strftime('%d %b %Y')
        local_dt = fields.Datetime.context_timestamp(self, value)
        return local_dt.strftime('%d %b %Y, %I:%M %p')

    def get_tracking_status_label(self):
        """Customer-facing status badge/panel label for /track."""
        self.ensure_one()
        label = self._PUBLIC_STATUS_LABELS.get(self.state)
        if label:
            if self.is_return_journey and self.state == 'out_for_delivery':
                return _('Out for delivery to sender')
            return _(label)
        return dict(self._fields['state'].selection).get(self.state, self.state)

    def _public_timeline_label(self, event_type, is_return=None):
        """Neutral customer-facing label for a timeline event type."""
        if is_return is None:
            is_return = self.is_return_journey
        if is_return and event_type in self._PUBLIC_RETURN_TIMELINE_LABELS:
            return _(self._PUBLIC_RETURN_TIMELINE_LABELS[event_type])
        if event_type in self._PUBLIC_TIMELINE_LABELS:
            return _(self._PUBLIC_TIMELINE_LABELS[event_type])
        labels = dict(self.env['logistics.shipment.event']._fields['event_type'].selection)
        return labels.get(event_type, event_type)

    def _sanitize_public_timeline_detail(self, detail):
        """Strip billing jargon, actor names, and internal ops copy from /track details.

        Rewrites or drops legacy notes that mention wallet / free-return billing,
        "by <Name>" actor segments, or DE/QR claim jargon so customers only see
        hub names, status-safe notes, and COD facts without who collected.
        """
        if not detail:
            return ''
        text = detail.strip()
        if not text:
            return ''

        def _clean_fragment(part):
            part = (part or '').strip()
            if not part:
                return ''

            # Soften / drop known internal ops phrases (label already covers self-assign).
            part = re.sub(
                r'(?i)^DE self-assigned via QR/?claim\.?$',
                '',
                part,
            )
            part = re.sub(
                r'(?i)^Assigned to\s+.+?\s+for\s+.+?\s+leg\.?$',
                '',
                part,
            )
            part = re.sub(
                r'(?i)^Local same-district delivery\s*[—\-]\s*hub inventory skipped\.?$',
                '',
                part,
            )
            part = re.sub(
                r'(?i)^Physical pass-through at\s+(.+?)\s+recorded\.?$',
                r'\1',
                part,
            )
            part = re.sub(
                r'(?i)^Cancelled by\s+[^.]+\.?$',
                '',
                part,
            )
            part = part.strip()
            if not part:
                return ''

            # "by Ashik - cash collected" / "by Ashik — cash collected" → keep fact only
            if re.match(r'(?i)^by\s+.+?\s*[-–—]\s*cash collected$', part):
                return 'Cash collected'
            if re.match(r'(?i)^cash collected$', part):
                return 'Cash collected'

            # Drop pure "by <Name>" actor fragments (and trailing dash variants)
            if re.match(r'(?i)^by\s+\S', part):
                # Keep non-name remainder after "by Name - "
                remainder = re.sub(
                    r'(?i)^by\s+[^·•\n-]+?\s*[-–—]\s*',
                    '',
                    part,
                ).strip()
                if remainder and remainder.lower() != part.lower():
                    if remainder.lower() == 'cash collected':
                        return 'Cash collected'
                    return remainder
                return ''

            # Inline " … by Name" (legacy notes without · separators)
            part = re.sub(
                r'(?i)\s+by\s+[A-Za-z][A-Za-z .\'-]{0,60}$',
                '',
                part,
            ).strip()
            return part

        fragments = [
            _clean_fragment(p)
            for p in re.split(r'\s*[·•]\s*', text)
        ]
        fragments = [f for f in fragments if f]

        billing_markers = (
            'wallet',
            'no charge',
            'free —',
            'free -',
            'free return',
            '(free',
            'no wallet',
            'seller-initiated',
            'hub custody',
        )
        kept = []
        for part in fragments:
            part_lower = part.lower()
            if any(marker in part_lower for marker in billing_markers):
                continue
            kept.append(part)
        return ' · '.join(kept)

    def get_tracking_origin_label(self):
        """Human-readable origin: district/state, else source hub, else pincode."""
        self.ensure_one()
        parts = []
        if self.shipping_from_district_id:
            parts.append(self.shipping_from_district_id.name)
        elif self.source_hub_id:
            parts.append(self.source_hub_id.name)
        if self.shipping_from_state_id:
            parts.append(self.shipping_from_state_id.name)
        if parts:
            return ', '.join(parts)
        return self.shipping_from_zip or _('—')

    def get_tracking_destination_label(self):
        """Human-readable destination: district/state, else dest hub, else pincode."""
        self.ensure_one()
        parts = []
        if self.shipping_to_district_id:
            parts.append(self.shipping_to_district_id.name)
        elif self.destination_hub_id:
            parts.append(self.destination_hub_id.name)
        if self.shipping_to_state_id:
            parts.append(self.shipping_to_state_id.name)
        if parts:
            return ', '.join(parts)
        return self.shipping_to_zip or _('—')

    def get_tracking_delivery_display(self):
        """Label + value for the delivery date panel (actual vs estimated)."""
        self.ensure_one()
        delivered_at = self.delivered_on or self.actual_delivery_date

        if self.state == 'returned':
            return {
                'label': _('Returned on'),
                'value': self._format_tracking_datetime(delivered_at) if delivered_at else _('Returned'),
                'is_delivered': True,
            }

        # Mid return journey: do not show the original outbound "Delivered on".
        if self.is_return_journey and self.state not in ('delivered', 'returned'):
            if self.estimated_delivery_date:
                return {
                    'label': _('Estimated return'),
                    'value': self.estimated_delivery_date.strftime('%d %b %Y'),
                    'is_delivered': False,
                }
            return {
                'label': _('Return in progress'),
                'value': False,
                'is_delivered': False,
            }

        if self.state == 'delivered' or delivered_at:
            return {
                'label': _('Delivered on'),
                'value': self._format_tracking_datetime(delivered_at) if delivered_at else _('Delivered'),
                'is_delivered': True,
            }
        if self.estimated_delivery_date:
            return {
                'label': _('Estimated Delivery'),
                'value': self.estimated_delivery_date.strftime('%d %b %Y'),
                'is_delivered': False,
            }
        return {
            'label': _('Estimated Delivery'),
            'value': False,  # omit "Pending" in UI
            'is_delivered': False,
        }

    def get_tracking_progress_steps(self):
        """Vertical journey steps with done / current / pending status."""
        self.ensure_one()
        current_idx = self._STATE_TO_PROGRESS_INDEX.get(self.state, 0)
        steps = []
        for idx, (_key, label) in enumerate(self._TRACKING_PROGRESS_STEPS):
            if self.state == 'delivered':
                status = 'done'
            elif current_idx < 0:
                # Cancelled / return: mark early steps done when we know pickup happened
                if idx == 0:
                    status = 'done'
                elif idx == 1 and (self.picked_on or self.state in ('return_picked', 'returned')):
                    status = 'done'
                else:
                    status = 'pending'
            elif idx < current_idx:
                status = 'done'
            elif idx == current_idx:
                status = 'current'
            else:
                status = 'pending'
            steps.append({
                'key': _key,
                'label': label,
                'status': status,
            })
        return steps

    def _synthesize_tracking_timeline(self, public=False):
        """Minimal timeline from known dates/state when no custody events exist.

        Only include steps we can back with real timestamps (or current state),
        so delivered packages never show an empty history.
        When public=True, omit DE names from synthesized detail lines.
        """
        self.ensure_one()
        entries = []
        order_dt = self.create_date
        if not order_dt and self.order_date:
            order_dt = fields.Datetime.to_datetime(self.order_date)
        if order_dt:
            entries.append({
                'label': _('Order Placed'),
                'time': order_dt,
                'time_display': self._format_tracking_datetime(order_dt),
                'detail': '',
                'event_type': 'order_added',
            })

        if self.pickup_requested_on:
            entries.append({
                'label': _('Pickup Requested'),
                'time': self.pickup_requested_on,
                'time_display': self._format_tracking_datetime(self.pickup_requested_on),
                'detail': '',
                'event_type': 'pickup_requested',
            })

        past_pickup_states = (
            'picked', 'in_transit', 'at_source_hub', 'at_central_hub',
            'at_destination_hub', 'out_for_delivery', 'delivered',
            'return_picked', 'returned',
        )
        if self.picked_on or self.state in past_pickup_states:
            picked_dt = self.picked_on or order_dt or self.write_date
            detail = self.source_hub_id.name if self.source_hub_id else ''
            entries.append({
                'label': _('Picked Up'),
                'time': picked_dt,
                'time_display': self._format_tracking_datetime(picked_dt),
                'detail': detail,
                'event_type': 'pickup_scan',
            })

        # Current hub / in-transit status (only when not yet delivered)
        if self.state in ('at_source_hub', 'at_central_hub', 'at_destination_hub', 'in_transit'):
            hub = self.current_hub_id
            if self.state == 'at_source_hub':
                hub_label = _('At Source Hub')
                hub = hub or self.source_hub_id
            elif self.state == 'at_central_hub':
                hub_label = _('At Central Hub')
            elif self.state == 'at_destination_hub':
                hub_label = _('At Destination Hub')
                hub = hub or self.destination_hub_id
            else:
                hub_label = _('In Transit')
            hub_dt = self.write_date or self.picked_on or order_dt
            entries.append({
                'label': hub_label,
                'time': hub_dt,
                'time_display': self._format_tracking_datetime(hub_dt),
                'detail': hub.name if hub else '',
                'event_type': 'hub_receive' if self.state != 'in_transit' else 'depart_hub',
            })

        if self.state == 'out_for_delivery':
            de_name = self.delivery_executive_id.name if self.delivery_executive_id else ''
            ofd_dt = self.write_date or self.picked_on or order_dt
            ofd_detail = ''
            if de_name and not public:
                ofd_detail = _('by %s') % de_name
            entries.append({
                'label': _('Out for Delivery'),
                'time': ofd_dt,
                'time_display': self._format_tracking_datetime(ofd_dt),
                'detail': ofd_detail,
                'event_type': 'out_for_delivery',
            })

        delivered_at = self.delivered_on or self.actual_delivery_date
        if self.state == 'returned':
            entries.append({
                'label': _('Returned to sender'),
                'time': delivered_at or self.write_date,
                'time_display': self._format_tracking_datetime(delivered_at or self.write_date),
                'detail': '',
                'event_type': 'returned',
            })
        elif self.state == 'delivered' or (delivered_at and not self.is_return_journey):
            entries.append({
                'label': _('Delivered'),
                'time': delivered_at or self.write_date,
                'time_display': self._format_tracking_datetime(delivered_at or self.write_date),
                'detail': '',
                'event_type': 'delivered',
            })

        if self.state == 'cancelled':
            entries.append({
                'label': _('Cancelled'),
                'time': self.write_date,
                'time_display': self._format_tracking_datetime(self.write_date),
                'detail': '',
                'event_type': 'cancelled',
            })

        return entries

    # Advancement rank for collapsing same-minute clusters (higher = more advanced).
    _TIMELINE_EVENT_RANK = {
        'order_added': 10,
        'pickup_requested': 20,
        'pickup_scan': 30,
        'dropped_at_hub': 40,
        'hub_receive': 50,
        'central_pass_through': 55,
        'hub_dispatch': 60,
        'depart_hub': 65,
        'leg_assign': 70,
        'de_self_assign': 75,
        'out_for_delivery': 80,
        'delivered': 90,
        'return_requested': 92,
        'returned': 96,
        'cancelled': 95,
        'status_override': 50,
        'note': 5,
        'skip_hub_local': 78,
    }

    def _timeline_minute_key(self, value):
        """Local (year, month, day, hour, minute) for same-minute clustering."""
        if not value:
            return None
        if isinstance(value, str):
            value = fields.Datetime.from_string(value)
        if not hasattr(value, 'hour'):
            return (value.year, value.month, value.day, 0, 0)
        local_dt = fields.Datetime.context_timestamp(self, value)
        return (local_dt.year, local_dt.month, local_dt.day, local_dt.hour, local_dt.minute)

    def _merge_timeline_cluster(self, cluster):
        """Collapse a same-minute cluster into one entry.

        Title = most advanced status; earlier labels + hub/actor notes go in detail.
        """
        if len(cluster) == 1:
            return cluster[0]

        def _rank(entry):
            return self._TIMELINE_EVENT_RANK.get(entry.get('event_type'), 50)

        def _time_sort_key(entry):
            return entry.get('time') or fields.Datetime.from_string('1970-01-01 00:00:00')

        primary = max(cluster, key=lambda e: (_rank(e), _time_sort_key(e)))
        earlier_labels = [
            e.get('label') for e in cluster
            if e is not primary and e.get('label')
        ]
        detail_parts = []
        if earlier_labels:
            detail_parts.append(' · '.join(earlier_labels))
        for e in cluster:
            detail = (e.get('detail') or '').strip()
            if detail and detail not in detail_parts:
                detail_parts.append(detail)

        merged = dict(primary)
        merged['detail'] = ' · '.join(detail_parts) if detail_parts else ''
        # Prefer the latest timestamp in the cluster for display consistency
        latest = max(cluster, key=_time_sort_key)
        if latest.get('time'):
            merged['time'] = latest['time']
            merged['time_display'] = self._format_tracking_datetime(latest['time'])
        return merged

    def _collapse_same_minute_timeline(self, entries):
        """Merge consecutive timeline items that share the same local minute."""
        if not entries or len(entries) < 2:
            return entries

        collapsed = []
        cluster = [entries[0]]
        for entry in entries[1:]:
            prev_key = self._timeline_minute_key(cluster[-1].get('time'))
            curr_key = self._timeline_minute_key(entry.get('time'))
            if prev_key is not None and curr_key is not None and prev_key == curr_key:
                cluster.append(entry)
            else:
                collapsed.append(self._merge_timeline_cluster(cluster))
                cluster = [entry]
        collapsed.append(self._merge_timeline_cluster(cluster))
        return collapsed

    def get_tracking_timeline(self, newest_first=False, public=False):
        """Timeline entries for public track / DE portal.

        Prefer real custody events; otherwise synthesize from known dates/state.
        Default order is chronological (oldest → newest).
        Consecutive same-minute items are collapsed into one history entry.

        When public=True (/track only): customer-facing labels, no actor names,
        and sanitized notes (billing / "by Name" / QR-claim jargon stripped).
        Portal callers should leave public=False so DE/hub names remain visible.
        """
        self.ensure_one()
        if self.event_ids:
            type_labels = dict(
                self.env['logistics.shipment.event']._fields['event_type'].selection
            )
            events = self.event_ids.sorted(
                key=lambda e: (e.event_time, e.id),
            )
            return_start = False
            return_req = self.event_ids.filtered(
                lambda e: e.event_type == 'return_requested'
            ).sorted(lambda e: (e.event_time, e.id))[:1]
            if return_req:
                return_start = return_req.event_time
            entries = []
            for event in events:
                is_return_event = bool(
                    event.event_type in ('return_requested', 'returned')
                    or (return_start and event.event_time >= return_start)
                    or (
                        self.is_return_journey
                        and not return_start
                        and event.event_type in (
                            'pickup_scan', 'out_for_delivery', 'hub_receive',
                            'hub_dispatch', 'depart_hub', 'dropped_at_hub',
                            'de_self_assign', 'skip_hub_local',
                        )
                    )
                )
                detail = event.get_timeline_detail(public=public) or ''
                if public:
                    label = self._public_timeline_label(
                        event.event_type, is_return=is_return_event
                    )
                    detail = self._sanitize_public_timeline_detail(detail)
                else:
                    label = type_labels.get(event.event_type, event.event_type)
                entries.append({
                    'label': label,
                    'time': event.event_time,
                    'time_display': self._format_tracking_datetime(event.event_time),
                    'detail': detail,
                    'event_type': event.event_type,
                })
        else:
            entries = self._synthesize_tracking_timeline(public=public)
            if public:
                for entry in entries:
                    entry['label'] = self._public_timeline_label(
                        entry.get('event_type'),
                        is_return=self.is_return_journey and entry.get('event_type') in (
                            'return_requested', 'returned', 'pickup_scan', 'out_for_delivery',
                        ),
                    )
                    entry['detail'] = self._sanitize_public_timeline_detail(
                        entry.get('detail') or ''
                    )

        entries = self._collapse_same_minute_timeline(entries)
        if public:
            for entry in entries:
                entry['detail'] = self._sanitize_public_timeline_detail(
                    entry.get('detail') or ''
                )

        if newest_first:
            entries = list(reversed(entries))
        return entries

    # -------------------------------------------------------------------------
    # Route leg helpers (Phase 2)
    # -------------------------------------------------------------------------
    def _get_next_actionable_leg(self):
        """First leg still in planned / assigned / in_progress."""
        self.ensure_one()
        return self.estimated_route_ids.sorted('sequence').filtered(
            lambda l: l.state in ('planned', 'assigned', 'in_progress')
        )[:1]

    def _get_claimable_leg(self, de=None):
        """Active/next leg that can be claimed at a hub (unassigned, or assigned to this DE)."""
        self.ensure_one()
        leg = self.active_leg_id
        if not leg or leg.state in ('done', 'skipped'):
            leg = self._get_next_actionable_leg()
        if not leg or leg.state not in ('planned', 'assigned'):
            return self.env['logistics.shipment.estimated.route']
        if not leg.assigned_de_id:
            return leg
        if de and leg.assigned_de_id == de:
            return leg
        # Already assigned to someone else — not claimable
        return self.env['logistics.shipment.estimated.route']

    def _sync_active_leg(self):
        """Point active_leg_id at the next actionable leg (or False)."""
        for shipment in self:
            next_leg = shipment._get_next_actionable_leg()
            if shipment.active_leg_id != next_leg:
                shipment.active_leg_id = next_leg.id if next_leg else False

    @api.model
    def _de_eligible_for_operation(self, de, operation_type):
        """Role check: if any role flag is set, enforce; if none set, allow all."""
        if not de:
            return False
        has_any_role = de.is_pickup or de.is_driver or de.is_delivery or de.is_manager
        if not has_any_role:
            return True
        if operation_type == 'pickup':
            return bool(de.is_pickup)
        if operation_type == 'hub_transfer':
            return bool(de.is_driver)
        if operation_type == 'delivery':
            return bool(de.is_delivery)
        return False

    def _de_eligible_for_leg(self, de, leg):
        self.ensure_one()
        if not de or not leg:
            return False
        return self._de_eligible_for_operation(de, leg.operation_type)

    def _assign_leg_de(self, leg, de, start=False):
        """Assign DE to a leg and optionally start it."""
        self.ensure_one()
        if not leg or not de:
            raise UserError(_("Leg and delivery executive are required for assignment."))
        if not self._de_eligible_for_leg(de, leg):
            raise UserError(
                _("Delivery executive %s is not eligible for %s legs.")
                % (de.name, leg.operation_type)
            )
        if leg.assigned_de_id and leg.assigned_de_id != de and leg.state not in ('planned',):
            raise UserError(
                _("Leg '%s' is already assigned to %s.")
                % (leg.name, leg.assigned_de_id.name)
            )
        vals = {
            'assigned_de_id': de.id,
            'state': 'in_progress' if start else 'assigned',
        }
        if start:
            vals['started_at'] = fields.Datetime.now()
        leg.write(vals)
        self.active_leg_id = leg.id
        return leg

    def _start_leg(self, leg, de=None):
        self.ensure_one()
        if not leg:
            return
        vals = {
            'state': 'in_progress',
            'started_at': leg.started_at or fields.Datetime.now(),
        }
        if de:
            vals['assigned_de_id'] = de.id
        leg.write(vals)
        self.active_leg_id = leg.id

    def _complete_leg(self, leg=None):
        """Mark leg done and advance active_leg_id."""
        self.ensure_one()
        leg = leg or self.active_leg_id
        if leg and leg.state != 'done':
            leg.write({
                'state': 'done',
                'completed_at': fields.Datetime.now(),
            })
        self._sync_active_leg()

    def can_de_self_assign(self, de):
        """Whether DE may claim this shipment via QR/self-assign at a hub."""
        self.ensure_one()
        if not de or not de.active:
            return False
        if self.custodian_type != 'hub' or not self.current_hub_id:
            return False
        if self.state in ('delivered', 'cancelled', 'returned'):
            return False
        leg = self._get_claimable_leg(de)
        if not leg:
            return False
        # Only hub_transfer / delivery are claimed from hub inventory
        if leg.operation_type == 'pickup':
            return False
        # Leg from_hub should match current hub when set
        if leg.from_hub_id and leg.from_hub_id != self.current_hub_id:
            return False
        return self._de_eligible_for_leg(de, leg)

    def action_de_self_assign(self, de, scanned_code=None, note=None):
        """DE claims package at hub for the next hop; transfers custody via hub dispatch."""
        if not de:
            raise UserError(_("A delivery executive is required to self-assign."))
        for shipment in self:
            if not shipment.can_de_self_assign(de):
                raise UserError(
                    _("You are not eligible to self-assign shipment %s.")
                    % shipment.name
                )
            leg = shipment._get_claimable_leg(de)
            if not leg:
                raise UserError(_("No claimable route leg on shipment %s.") % shipment.name)

            shipment._assign_leg_de(leg, de, start=True)
            for_delivery = leg.operation_type == 'delivery'
            # Reuse hub dispatch custody transition; log as de_self_assign
            shipment.action_hub_dispatch(
                delivery_executive=de,
                hub=shipment.current_hub_id,
                scanned_code=scanned_code or shipment.name,
                note=note or _("DE self-assigned via QR/claim."),
                for_delivery=for_delivery,
                skip_leg_assign=True,
                event_type_override='de_self_assign',
            )
        return True

    def action_request_return(self):
        """Seller-initiated free return: reverse route customer → hubs → seller.

        No wallet debit at any step. Rebuilds estimated route with swapped hubs
        so existing hub receive/dispatch/self-assign custody actions apply.
        """
        for shipment in self:
            if shipment.state != 'delivered':
                raise UserError(
                    _("Shipment %s can only request return when Delivered "
                      "(current state: %s).")
                    % (shipment.name, shipment.state)
                )
            if shipment.is_return_journey:
                raise UserError(
                    _("Shipment %s already has a return journey in progress.")
                    % shipment.name
                )

            # Unlock and rebuild reverse planned route (no wallet charge)
            shipment.write({
                'is_return_journey': True,
                'route_locked': False,
            })
            shipment.estimated_route_ids = [(5, 0, 0)]
            shipment.with_context(rebuild_return_route=True)._compute_estimated_route_ids()
            shipment._sync_active_leg()

            pickup_leg = shipment.estimated_route_ids.filtered(
                lambda l: l.operation_type == 'pickup'
            )[:1]
            pickup_de = pickup_leg.executive1_id if pickup_leg else False
            if pickup_de and pickup_leg:
                shipment._assign_leg_de(pickup_leg, pickup_de, start=False)

            shipment._create_custody_event(
                'return_requested',
                to_custodian='customer',
                note=False,
                leg=pickup_leg,
            )
            shipment._write_with_state({
                'state': 'return_requested',
                'custodian_type': 'customer',
                'custodian_de_id': False,
                'current_hub_id': False,
                'delivery_executive_id': pickup_de.id if pickup_de else False,
                'active_leg_id': pickup_leg.id if pickup_leg else False,
                'route_locked': True,
            })
        return True

    def action_mark_picked(self, actor_de=None, scanned_code=None, note=None):
        """DE confirms pickup from seller (outbound) or customer (return). Custody → DE."""
        for shipment in self:
            outbound_ok = shipment.state in ('pickup_requested', 'order_added')
            return_ok = shipment.state == 'return_requested' and shipment.is_return_journey
            if not (outbound_ok or return_ok):
                raise UserError(
                    _("Shipment %s cannot be marked picked from state '%s'.")
                    % (shipment.name, shipment.state)
                )
            de = actor_de or shipment.delivery_executive_id or shipment.custodian_de_id
            shipment._sync_active_leg()
            pickup_leg = shipment.active_leg_id
            if pickup_leg and pickup_leg.operation_type != 'pickup':
                pickup_leg = shipment.estimated_route_ids.filtered(
                    lambda l: l.operation_type == 'pickup' and l.state != 'done'
                )[:1]
            if de and pickup_leg:
                if pickup_leg.assigned_de_id and pickup_leg.assigned_de_id != de:
                    raise UserError(
                        _("Pickup leg is assigned to %s.")
                        % pickup_leg.assigned_de_id.name
                    )
                if not shipment._de_eligible_for_leg(de, pickup_leg):
                    raise UserError(
                        _("Delivery executive %s is not eligible for pickup.")
                        % de.name
                    )
                shipment._assign_leg_de(pickup_leg, de, start=True)
            elif pickup_leg and de:
                shipment._start_leg(pickup_leg, de=de)

            pickup_note = note
            if return_ok and not pickup_note:
                pickup_note = False
            shipment._create_custody_event(
                'pickup_scan',
                to_custodian='de',
                actor_de=de,
                scanned_code=scanned_code or shipment.name,
                note=pickup_note,
                leg=pickup_leg,
            )
            vals = {
                'state': 'return_picked' if return_ok else 'picked',
                'picked_on': fields.Datetime.now(),
                'custodian_type': 'de',
                'custodian_de_id': de.id if de else False,
                'current_hub_id': False,
                'estimated_delivery_date': fields.Date.today(),
            }
            if de and not shipment.delivery_executive_id:
                vals['delivery_executive_id'] = de.id
            if pickup_leg:
                vals['active_leg_id'] = pickup_leg.id
            shipment._write_with_state(vals)
            shipment._lock_route()
        return True

    def _is_north_south_cross_zone(self):
        """True when seller and customer districts are opposite Kerala zones (north↔south)."""
        self.ensure_one()
        from_district = self.shipping_from_district_id
        to_district = self.shipping_to_district_id
        if not from_district or not to_district:
            return False
        District = self.env['logistics.district']
        from_zone = District.get_district_zone(from_district)
        to_zone = District.get_district_zone(to_district)
        return {from_zone, to_zone} == {'north', 'south'}

    def get_portal_drop_hub_ids(self):
        """Hubs allowed in portal drop/receive selectors for this shipment.

        Always includes pickup (source) and drop (destination) hubs when set,
        plus the active transfer leg's from/to hubs. Includes the main hub
        (Thrissur) only when origin/destination districts are north↔south.
        """
        self.ensure_one()
        Hub = self.env['logistics.hub']
        hubs = Hub.browse()
        if self.source_hub_id:
            hubs |= self.source_hub_id
        if self.destination_hub_id:
            hubs |= self.destination_hub_id
        leg = self.active_leg_id
        if leg:
            if leg.from_hub_id:
                hubs |= leg.from_hub_id
            if leg.to_hub_id:
                hubs |= leg.to_hub_id
        if self._is_north_south_cross_zone():
            main_hub = Hub.get_main_hub()
            if main_hub:
                hubs |= main_hub
        return hubs.filtered(lambda h: h.active)

    def get_preferred_portal_drop_hub(self):
        """Expected next drop hub: active leg to_hub, else source (pickup) hub."""
        self.ensure_one()
        leg = self.active_leg_id
        if leg and leg.operation_type in ('hub_transfer', 'pickup') and leg.to_hub_id:
            return leg.to_hub_id
        return self.source_hub_id or False

    def action_drop_at_hub(self, hub=None, actor_de=None, scanned_code=None, note=None):
        """DE marks package dropped at a hub.

        Custody remains with the DE until hub_receive — hub manager scan is
        the source of truth that transfers custodian_type to hub.
        """
        for shipment in self:
            if shipment.custodian_type != 'de':
                raise UserError(
                    _("Shipment %s must be in DE custody to drop at hub (current: %s).")
                    % (shipment.name, shipment.custodian_type)
                )
            if shipment.state not in (
                'picked', 'return_picked', 'in_transit',
                'at_source_hub', 'at_central_hub', 'at_destination_hub',
            ):
                raise UserError(
                    _("Shipment %s cannot be dropped at hub from state '%s'.")
                    % (shipment.name, shipment.state)
                )
            leg = shipment.active_leg_id
            if hub:
                target_hub = hub
            elif leg and leg.operation_type == 'hub_transfer' and leg.to_hub_id:
                target_hub = leg.to_hub_id
            elif leg and leg.operation_type == 'pickup' and leg.to_hub_id:
                target_hub = leg.to_hub_id
            else:
                target_hub = shipment.source_hub_id or shipment.current_hub_id or shipment.destination_hub_id
            if not target_hub:
                raise UserError(_("No hub specified for drop of shipment %s.") % shipment.name)
            allowed = shipment.get_portal_drop_hub_ids()
            if target_hub not in allowed:
                raise UserError(
                    _("Hub '%s' is not a valid drop location for shipment %s. "
                      "Allowed: %s.")
                    % (
                        target_hub.name,
                        shipment.name,
                        ', '.join(allowed.mapped('name')) or _('none'),
                    )
                )
            de = actor_de or shipment.custodian_de_id or shipment.delivery_executive_id
            shipment._create_custody_event(
                'dropped_at_hub',
                to_custodian='de',
                hub=target_hub,
                actor_de=de,
                scanned_code=scanned_code or shipment.name,
                note=note or _("Dropped at hub; awaiting hub receive scan."),
            )
            # Stay in DE custody; optionally move toward in_transit if leaving pickup area
            vals = {
                'current_hub_id': target_hub.id,
            }
            if shipment.state in ('picked', 'return_picked'):
                vals['state'] = 'in_transit'
            shipment._write_with_state(vals)
            shipment._lock_route()
        return True

    def action_hub_receive(self, hub=None, scanned_code=None, note=None):
        """Hub manager receive scan — source of truth for hub custody."""
        for shipment in self:
            target_hub = hub or shipment.current_hub_id or shipment.source_hub_id
            if not target_hub:
                raise UserError(_("Hub is required to receive shipment %s.") % shipment.name)
            if shipment.state in ('delivered', 'cancelled', 'returned'):
                raise UserError(
                    _("Shipment %s cannot be received at hub in state '%s'.")
                    % (shipment.name, shipment.state)
                )
            allowed = shipment.get_portal_drop_hub_ids()
            if shipment.current_hub_id:
                allowed |= shipment.current_hub_id
            if target_hub not in allowed:
                raise UserError(
                    _("Hub '%s' is not a valid receive location for shipment %s. "
                      "Allowed: %s.")
                    % (
                        target_hub.name,
                        shipment.name,
                        ', '.join(allowed.mapped('name')) or _('none'),
                    )
                )

            # Determine hub-stop state from planned route hubs
            if target_hub == shipment.source_hub_id and target_hub == shipment.destination_hub_id:
                new_state = 'at_source_hub'
            elif target_hub == shipment.source_hub_id:
                new_state = 'at_source_hub'
            elif target_hub == shipment.destination_hub_id:
                new_state = 'at_destination_hub'
            elif target_hub.hub_type == 'main':
                # Optional physical pass-through at Thrissur — log presence without forcing route
                new_state = 'at_central_hub'
            else:
                new_state = 'at_source_hub'

            # Complete the inbound leg (pickup arriving at source, or hub_transfer arriving at dest)
            inbound_leg = shipment.active_leg_id
            if inbound_leg and inbound_leg.operation_type in ('pickup', 'hub_transfer'):
                if inbound_leg.to_hub_id and inbound_leg.to_hub_id != target_hub:
                    # Arriving at unexpected hub — still complete if in progress
                    pass
                if inbound_leg.state in ('planned', 'assigned', 'in_progress'):
                    shipment._complete_leg(inbound_leg)
            else:
                shipment._sync_active_leg()

            shipment._create_custody_event(
                'hub_receive',
                to_custodian='hub',
                hub=target_hub,
                scanned_code=scanned_code or shipment.name,
                note=note,
            )
            shipment._write_with_state({
                'state': new_state,
                'custodian_type': 'hub',
                'custodian_de_id': False,
                'current_hub_id': target_hub.id,
                'delivery_executive_id': False,  # clear assigned DE until redispatch
            })
            shipment._sync_active_leg()
            shipment._lock_route()
        return True

    def action_hub_dispatch(self, delivery_executive, hub=None, scanned_code=None, note=None,
                            for_delivery=True, skip_leg_assign=False, event_type_override=None):
        """Hub assigns a DE and releases custody (dispatch / out for delivery or transfer)."""
        if not delivery_executive:
            raise UserError(_("A delivery executive is required to dispatch."))
        for shipment in self:
            if shipment.custodian_type != 'hub':
                raise UserError(
                    _("Shipment %s must be in hub custody to dispatch (current: %s).")
                    % (shipment.name, shipment.custodian_type)
                )
            target_hub = hub or shipment.current_hub_id
            if not target_hub:
                raise UserError(_("Current hub is missing on shipment %s.") % shipment.name)

            shipment._sync_active_leg()
            leg = shipment.active_leg_id
            if leg and leg.operation_type == 'pickup':
                # Should not dispatch pickup from hub; advance if stale
                shipment._sync_active_leg()
                leg = shipment.active_leg_id

            if not skip_leg_assign and leg:
                if leg.assigned_de_id and leg.assigned_de_id != delivery_executive:
                    raise UserError(
                        _("Leg '%s' is already assigned to %s. Clear assignee first.")
                        % (leg.name, leg.assigned_de_id.name)
                    )
                if not shipment._de_eligible_for_leg(delivery_executive, leg):
                    raise UserError(
                        _("Delivery executive %s is not eligible for %s legs.")
                        % (delivery_executive.name, leg.operation_type)
                    )
                shipment._assign_leg_de(leg, delivery_executive, start=True)
                for_delivery = leg.operation_type == 'delivery'
            elif not skip_leg_assign and not leg:
                # No planned legs — fall back to for_delivery flag
                pass
            elif skip_leg_assign and leg:
                for_delivery = leg.operation_type == 'delivery'

            # Same-district last mile or dest-hub last mile → out_for_delivery;
            # otherwise in_transit for hub-to-hub movement.
            if for_delivery or (leg and leg.operation_type == 'delivery') or (
                not leg and target_hub == shipment.destination_hub_id
            ):
                new_state = 'out_for_delivery'
                event_type = 'out_for_delivery'
            else:
                new_state = 'in_transit'
                event_type = 'hub_dispatch'

            if event_type_override:
                event_type = event_type_override

            shipment._create_custody_event(
                event_type,
                to_custodian='de',
                hub=target_hub,
                actor_de=delivery_executive,
                scanned_code=scanned_code or shipment.name,
                note=note,
                leg=leg,
            )
            shipment._write_with_state({
                'state': new_state,
                'custodian_type': 'de',
                'custodian_de_id': delivery_executive.id,
                'delivery_executive_id': delivery_executive.id,
                'current_hub_id': target_hub.id if new_state == 'in_transit' else False,
                'active_leg_id': leg.id if leg else (shipment.active_leg_id.id if shipment.active_leg_id else False),
            })
            shipment._lock_route()
        return True

    def get_active_leg_label(self):
        """Human-readable active leg label for portal / My Tasks."""
        self.ensure_one()
        leg = self.active_leg_id
        if not leg:
            return False
        if leg.operation_type == 'hub_transfer':
            from_hub = leg.from_hub_id
            to_hub = leg.to_hub_id
            from_label = (from_hub.code or from_hub.name) if from_hub else (leg.source_location_name or '?')
            to_label = (to_hub.code or to_hub.name) if to_hub else (leg.destination_location_name or '?')
            return _("Hub transfer: %s → %s") % (from_label, to_label)
        if leg.operation_type == 'pickup':
            return _("Pickup → %s") % (
                (leg.to_hub_id.code or leg.to_hub_id.name) if leg.to_hub_id else (leg.destination_location_name or 'Hub')
            )
        if leg.operation_type == 'delivery':
            return _("Last mile delivery")
        return leg.name or False

    def can_depart_from_hub(self, de=None):
        """DE still at source hub on an in-progress hub_transfer leg."""
        self.ensure_one()
        if self.custodian_type != 'de' or self.state != 'in_transit':
            return False
        if de and self.custodian_de_id and self.custodian_de_id != de:
            return False
        leg = self.active_leg_id
        if not leg or leg.operation_type != 'hub_transfer' or leg.state not in ('assigned', 'in_progress'):
            return False
        from_hub = leg.from_hub_id or self.source_hub_id
        return bool(self.current_hub_id and from_hub and self.current_hub_id == from_hub)

    def action_depart_from_hub(self, actor_de=None, note=None):
        """DE confirms leaving the source hub on a hub_transfer (stays in_transit)."""
        for shipment in self:
            de = actor_de or shipment.custodian_de_id or shipment.delivery_executive_id
            if not shipment.can_depart_from_hub(de):
                raise UserError(
                    _("Shipment %s cannot record hub departure in its current state.")
                    % shipment.name
                )
            left_hub = shipment.current_hub_id
            shipment._create_custody_event(
                'depart_hub',
                to_custodian='de',
                hub=left_hub,
                actor_de=de,
                scanned_code=shipment.name,
                note=note or _("Departed %s for hub transfer.") % (left_hub.name if left_hub else _('hub')),
                leg=shipment.active_leg_id,
            )
            shipment._write_with_state({
                'state': 'in_transit',
                'current_hub_id': False,
            })
            shipment._lock_route()
        return True

    def can_record_central_pass_through(self, de=None):
        """Optional Thrissur pass-through while DE holds a mid hub-transfer package.

        When ``de`` is None (admin/backend), allow any non-terminal shipment.
        """
        self.ensure_one()
        if self.state in ('delivered', 'cancelled', 'returned'):
            return False
        if de is None:
            return True
        if self.custodian_type != 'de':
            return False
        if self.custodian_de_id and self.custodian_de_id != de:
            return False
        if self.state not in ('in_transit', 'picked', 'at_central_hub'):
            return False
        leg = self.active_leg_id
        if leg and leg.operation_type == 'hub_transfer' and leg.state in ('assigned', 'in_progress'):
            return True
        return self.state == 'in_transit'

    def action_central_pass_through(self, hub=None, scanned_code=None, note=None, actor_de=None):
        """Optional Thrissur pass-through: keep in_transit + event + note location at main hub.

        Does not insert Thrissur into the planned route. Physical scan/event only.
        """
        main_hub = hub or self.env['logistics.hub'].get_main_hub()
        if not main_hub:
            raise UserError(_("No Main Hub (Thrissur) is configured."))
        if main_hub.hub_type != 'main':
            raise UserError(_("Pass-through must be recorded at the Main Hub (Thrissur)."))
        for shipment in self:
            if shipment.state in ('delivered', 'cancelled', 'returned'):
                raise UserError(
                    _("Shipment %s cannot record pass-through in state '%s'.")
                    % (shipment.name, shipment.state)
                )
            de = actor_de or shipment.custodian_de_id
            if actor_de is not None and not shipment.can_record_central_pass_through(actor_de):
                raise UserError(
                    _("Shipment %s is not eligible for Thrissur pass-through right now.")
                    % shipment.name
                )

            shipment._create_custody_event(
                'central_pass_through',
                to_custodian=shipment.custodian_type,
                hub=main_hub,
                actor_de=de,
                scanned_code=scanned_code or shipment.name,
                note=note or _("Physical pass-through at %s recorded.") % main_hub.name,
                leg=shipment.active_leg_id,
            )
            # Keep in_transit; location is on the event (hub_id), not as hub inventory presence
            if shipment.state != 'in_transit' and shipment.custodian_type == 'de':
                shipment._write_with_state({'state': 'in_transit'})
            shipment._lock_route()
        return True

    def action_cancel_shipment(self):
        """Admin cancel — preferred over free statusbar clicks."""
        for shipment in self:
            if shipment.state in ('delivered', 'returned'):
                raise UserError(
                    _("Shipment %s cannot be cancelled from state '%s'.")
                    % (shipment.name, shipment.state)
                )
            if shipment.state == 'cancelled':
                continue
            shipment._create_custody_event(
                'status_override',
                to_custodian=shipment.custodian_type,
                note=_("Cancelled by %s.") % self.env.user.name,
            )
            shipment._write_with_state({
                'state': 'cancelled',
            })
        return True

    def action_mark_delivered(self, actor_de=None, scanned_code=None, note=None, delivery_remarks=None):
        """DE marks delivered (outbound → customer) or returned (return → seller).

        Only allowed from out_for_delivery. Return completion is free — no wallet debit.
        """
        for shipment in self:
            if shipment.state != 'out_for_delivery':
                raise UserError(
                    _("Shipment %s can only be marked delivered when Out for Delivery "
                      "(current state: %s).")
                    % (shipment.name, shipment.state)
                )
            de = actor_de or shipment.custodian_de_id or shipment.delivery_executive_id
            delivery_leg = shipment.active_leg_id
            if delivery_leg and delivery_leg.operation_type != 'delivery':
                delivery_leg = shipment.estimated_route_ids.filtered(
                    lambda l: l.operation_type == 'delivery' and l.state != 'done'
                )[:1]
            is_return = shipment.is_return_journey
            event_note = note or delivery_remarks
            if is_return and not event_note:
                event_note = False
            shipment._create_custody_event(
                'returned' if is_return else 'delivered',
                to_custodian='seller' if is_return else 'customer',
                actor_de=de,
                scanned_code=scanned_code or shipment.name,
                note=event_note,
                leg=delivery_leg,
            )
            if delivery_leg:
                shipment._complete_leg(delivery_leg)
            vals = {
                'state': 'returned' if is_return else 'delivered',
                'custodian_type': 'seller' if is_return else 'customer',
                'custodian_de_id': False,
                'current_hub_id': False,
                'actual_delivery_date': fields.Datetime.now(),
                'delivered_on': fields.Datetime.now(),
                'active_leg_id': False,
            }
            if delivery_remarks is not None:
                vals['delivery_remarks'] = delivery_remarks
            shipment._write_with_state(vals)
        return True

    def can_skip_hub_local_delivery(self, de=None):
        """Same-district (same source/dest hub) packages can go OFD after pickup without hub inventory."""
        self.ensure_one()
        if self.state not in ('picked', 'return_picked') or self.custodian_type != 'de':
            return False
        if de and self.custodian_de_id and self.custodian_de_id != de:
            return False
        source = self.source_hub_id
        dest = self.destination_hub_id
        if not source or not dest or source != dest:
            return False
        # Cross-district always requires hubs
        if self.shipping_from_district_id and self.shipping_to_district_id:
            if self.shipping_from_district_id != self.shipping_to_district_id:
                return False
        return True

    def action_skip_hub_local_delivery(self, actor_de=None, scanned_code=None, note=None):
        """Same-district shortcut: after pickup, go directly out for delivery (skip hub inventory).

        Completes the pickup leg, marks any non-delivery leftover legs as skipped,
        starts the delivery leg, and sets custody/state to out_for_delivery.
        """
        for shipment in self:
            de = actor_de or shipment.custodian_de_id or shipment.delivery_executive_id
            if not shipment.can_skip_hub_local_delivery(de):
                raise UserError(
                    _("Shipment %s is not eligible for local delivery skip-hub "
                      "(requires same-district / same hub, picked/return_picked, DE custody).")
                    % shipment.name
                )
            shipment._sync_active_leg()
            pickup_leg = shipment.estimated_route_ids.filtered(
                lambda l: l.operation_type == 'pickup' and l.state != 'done'
            )[:1]
            delivery_leg = shipment.estimated_route_ids.filtered(
                lambda l: l.operation_type == 'delivery' and l.state not in ('done', 'skipped')
            )[:1]
            if pickup_leg:
                if pickup_leg.state in ('planned', 'assigned', 'in_progress'):
                    shipment._complete_leg(pickup_leg)
            # Skip any non-delivery legs still open (should be none on same-hub routes)
            for leg in shipment.estimated_route_ids.filtered(
                lambda l: l.operation_type != 'delivery' and l.state not in ('done', 'skipped')
            ):
                leg.write({
                    'state': 'skipped',
                    'completed_at': fields.Datetime.now(),
                })
            if delivery_leg:
                shipment._assign_leg_de(delivery_leg, de, start=True)
            shipment._create_custody_event(
                'skip_hub_local',
                to_custodian='de',
                hub=shipment.source_hub_id,
                actor_de=de,
                scanned_code=scanned_code or shipment.name,
                note=note or _("Local same-district delivery — hub inventory skipped."),
                leg=delivery_leg,
            )
            shipment._write_with_state({
                'state': 'out_for_delivery',
                'custodian_type': 'de',
                'custodian_de_id': de.id if de else False,
                'delivery_executive_id': de.id if de else shipment.delivery_executive_id.id,
                'current_hub_id': False,
                'active_leg_id': delivery_leg.id if delivery_leg else False,
            })
            shipment._lock_route()
        return True

    company_id = fields.Many2one('res.company', string='Company', required=True, default=lambda self: self.env.company)

    seller_id = fields.Many2one('logistics.seller', string='Seller', required=True)
    order_id = fields.Many2one('logistics.order', string='Order', ondelete='cascade')
    delivery_executive_id = fields.Many2one('logistics.delivery.executive', string='Delivery Executive')
    order_date = fields.Date(string='Order Date', required=True, default=fields.Date.context_today)

    @api.depends('seller_id')
    def _compute_shippping_from(self):
        for shipment in self:
            if shipment.seller_id:
                shipment.shipping_from_name = shipment.seller_id.name
                shipment.shipping_from_address = '\n'.join([shipment.seller_id.street or '', shipment.seller_id.street2 or '']) if shipment.seller_id.street or shipment.seller_id.street2 else ''
                shipment.shipping_from_zip = shipment.seller_id.zip
                shipment.shipping_from_district_id = shipment.seller_id.district_id
                shipment.shipping_from_state_id = shipment.seller_id.state_id
                shipment.shipping_from_country_id = shipment.seller_id.country_id

    # Shipping From Address
    shipping_from_name = fields.Char(string='Shipping From Name', compute='_compute_shippping_from', store=True, readonly=False)
    shipping_from_address = fields.Text(string='Shipping From Address', compute='_compute_shippping_from', store=True, readonly=False)
    shipping_from_zip = fields.Char(string='Shipping From Pincode', compute='_compute_shippping_from', store=True, readonly=False, required=True)
    @api.onchange('shipping_from_zip')
    def _onchange_shipping_from_zip(self):
        if self.shipping_from_zip:
            pincode_info = self.env['logistics.district'].get_district_from_pincode(self.shipping_from_zip)
            self.shipping_from_district_id = pincode_info['district_id'].id if pincode_info['district_id'] else False
            self.shipping_from_state_id = pincode_info['district_id'].state_id.id if pincode_info['district_id'] else False
    
    shipping_from_district_id = fields.Many2one('logistics.district', string='Shipping From District', compute='_compute_shippping_from', store=True, readonly=False)
    shipping_from_state_id = fields.Many2one('res.country.state', string='Shipping From State', compute='_compute_shippping_from', store=True, readonly=False)
    shipping_from_country_id = fields.Many2one('res.country', string='Shipping From Country', compute='_compute_shippping_from', store=True, readonly=False)

    # Shipping To Address
    shipping_to_name = fields.Char(string='Shipping To Name',)
    shipping_to_address = fields.Text(string='Shipping To Address')
    shipping_to_zip = fields.Char(string='Shipping To Pincode', required=True)
    @api.onchange('shipping_to_zip')
    def _onchange_shipping_to_zip(self):
        if self.shipping_to_zip:
            pincode_info = self.env['logistics.district'].get_district_from_pincode(self.shipping_to_zip)
            self.shipping_to_district_id = pincode_info['district_id'].id if pincode_info['district_id'] else False
            self.shipping_to_state_id = pincode_info['district_id'].state_id.id if pincode_info['district_id'] else False

    shipping_to_district_id = fields.Many2one('logistics.district', string='Shipping To District')
    shipping_to_state_id = fields.Many2one('res.country.state', string='Shipping To State', default=lambda self: self.env.company.state_id.id)
    shipping_to_country_id = fields.Many2one('res.country', string='Shipping To Country', default=lambda self: self.env.company.partner_id.country_id.id) 
    shipping_to_mobile = fields.Char(string='Shipping To Mobile Number')
    shipping_to_email = fields.Char(string='Shipping To Email')

    # Billing Address
    billing_same_as_shipping = fields.Boolean(string='Same as Shipping', default=True)
    billing_name = fields.Char(string='Billing Name',)
    billing_address = fields.Text(string='Billing Address')
    billing_zip = fields.Char(string='Billing Pincode')
    @api.onchange('billing_zip')
    def _onchange_billing_zip(self):
        if self.billing_zip:
            pincode_info = self.env['logistics.district'].get_district_from_pincode(self.billing_zip)
            self.billing_district_id = pincode_info['district_id'].id if pincode_info['district_id'] else False
            self.billing_state_id = pincode_info['district_id'].state_id.id if pincode_info['district_id'] else False

    billing_district_id = fields.Many2one('logistics.district', string='Billing District')
    billing_state_id = fields.Many2one('res.country.state', string='Billing State', default=lambda self: self.env.company.state_id.id)
    billing_country_id = fields.Many2one('res.country', string='Billing Country', default=lambda self: self.env.company.partner_id.country_id.id)

    @api.onchange('shipping_to_name', 'shipping_to_address', 'shipping_to_zip', 'shipping_to_district_id', 'shipping_to_state_id', 'shipping_to_country_id')
    def _onchange_shipping_to_address(self):
        if self.billing_same_as_shipping:
            self.billing_name = self.shipping_to_name
            self.billing_address = self.shipping_to_address
            self.billing_zip = self.shipping_to_zip
            self.billing_district_id = self.shipping_to_district_id
            self.billing_state_id = self.shipping_to_state_id
            self.billing_country_id = self.shipping_to_country_id

    estimated_delivery_date = fields.Date(string='Estimated Delivery Date')
    actual_delivery_date = fields.Datetime(string='Actual Delivery Date')
    delivery_remarks = fields.Text(string="Delivery Remarks")

    order_payment_type = fields.Selection([('prepaid', 'Prepaid'), ('cod', 'COD'), ('na', 'Not Applicable')], string='Order Payment Type', required=True, default='prepaid')
    cod_payment_method = fields.Selection([('cash', 'Cash'), ('upi', 'UPI')], string='COD Payment Method')

    delivery_charges_subtotal = fields.Monetary(string='Delivery Charges (Subtotal)', currency_field='currency_id', compute='_compute_delivery_charges', store=True, readonly=False)
    @api.depends('total_weight', 'shipping_from_district_id', 'shipping_to_district_id', 'tax_percentage')
    def _compute_delivery_charges(self):
        for record in self:
            if record.total_weight:
                same_district = (record.shipping_from_district_id == record.shipping_to_district_id)
                package_id = record.seller_id.delivery_package_id.id if record.seller_id.delivery_package_id else None
                record.delivery_charges_subtotal = self.env['logistics.delivery.charges'].sudo().calculate_delivery_charge(
                    record.total_weight, same_district, package_id=package_id)
            record.delivery_charges_total = record.delivery_charges_subtotal * (1 + record.tax_percentage) if record.tax_percentage else record.delivery_charges_subtotal
    delivery_charges_total = fields.Monetary(string='Delivery Charges (Incl. Tax)', currency_field='currency_id', compute='_compute_delivery_charges', store=True)
    tax_percentage = fields.Float(string='Tax Percentage', default=0)
    total_weight = fields.Float(string='Total Weight (Kg)', digits=(16, 3), default=0.0)
    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)

    item_description = fields.Text(string='Item Description')
    total_order_value = fields.Monetary(string='Total Order Amount', currency_field='currency_id', default=0.0)
    cod_amount = fields.Monetary(string='COD Amount', currency_field='currency_id', default=0.0)
    @api.onchange('order_payment_type')
    def _onchange_order_payment_type(self):
        if self.order_payment_type == 'prepaid':
            if self.cod_payment_transfer_ids:
                raise UserError("Payment method cannot be changed for orders with existing COD Payment Transfers. Please delete all the related Transfers before changing payment type.")
            self.cod_amount = 0.0
        elif self.order_payment_type == 'cod':
            self.cod_amount = self.total_order_value
        else:
            self.cod_amount = 0.0
    seller_notes = fields.Text(string='Seller Notes')

    pickup_requested_on = fields.Datetime(string='Pickup Requested On')
    picked_on = fields.Datetime(string='Picked On')
    delivered_on = fields.Datetime(string='Delivered On')

    state = fields.Selection(delivery_states, string='Delivery Status', default='order_added', tracking=True)

    wallet_transaction_id = fields.Many2one("logistics.wallet.transaction", string="Wallet Transaction (Legacy)")

    def action_add_wallet_transaction(self):
        if not self.seller_id:
            raise UserError(f'Seller must be set before adding Wallet Transaction!')
        if not self.seller_id.wallet_ids:
            raise UserError(f'No Wallets found for this Seller!')
        if not self.wallet_transaction_id:
            wallet = self.seller_id.wallet_ids[0]
            # Check wallet balance
            if wallet.balance < self.delivery_charges_total:
                raise UserError(f'Insufficient balance available in your Wallet. Current balance is {wallet.currency_id.format(wallet.balance)}. Please recharge before proceeding')
            
            self.wallet_transaction_id = self.env['logistics.wallet.transaction'].sudo().create({
                'wallet_id': wallet.id,
                'amount': -self.delivery_charges_total,
                'transaction_date': fields.Date.context_today(self),
                'shipment_id': self.id,
                'reference': self.display_name,
            }).id

    def delete_wallet_transaction(self):
        if not self.wallet_transaction_id:
            raise UserError(f'No transaction linked to this Shipment')
        self.wallet_transaction_id.unlink()

    def action_view_wallet_transaction(self):
        if self.wallet_transaction_id:
            return {
                'name': 'Wallet Transaction',
                'type': 'ir.actions.act_window',
                'res_model': 'logistics.wallet.transaction',
                'view_mode': 'list',
                'domain': [('id', '=', self.wallet_transaction_id.id)],
                'context': {'default_wallet_id': self.wallet_transaction_id.wallet_id.id},
            }

    cod_payment_transfer_ids = fields.Many2many("logistics.account.transfer", string="COD Payment Transfers")
    cod_paid_amount = fields.Monetary(string="COD Paid Amount", compute="_compute_cod_paid_balance_amount", store=True)
    cod_balance_amount = fields.Monetary(string="COD Balance Amount", compute="_compute_cod_paid_balance_amount", store=True)

    @api.depends('cod_payment_transfer_ids', 'cod_payment_transfer_ids.amount', 'cod_amount')
    def _compute_cod_paid_balance_amount(self):
        for rec in self:
            rec.cod_paid_amount = sum(rec.cod_payment_transfer_ids.mapped('amount'))
            rec.cod_balance_amount = rec.cod_amount - rec.cod_paid_amount

    def action_add_cod_payment_transfer(self):
        self.ensure_one()
        from_account = self.env['logistics.account'].search([('account_type', 'in', ('cod_customer'))], limit=1)
        if not from_account:
            raise UserError(f'No COD Customer Account found! Please create atleast on account of type COD Customer Account before proceeding.')
        to_account = self.env['logistics.account'].search([('account_type', 'in', ('bank', 'cash'))], limit=1)
        if not to_account:
            raise UserError(f'No Bank or Cash account found! Please create atleast one Bank or Cash account before proceeding.')
        from_account = from_account[0]
        to_account = to_account[0]
        return {
            'name': 'COD Payment Wizard',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.cod.payment.wizard',
            'view_mode': 'form',
            'context': {
                'default_shipment_id': self.id,
                'default_amount': self.cod_balance_amount,
                'default_from_account_id': from_account.id,
                'default_to_account_id': to_account.id,
                'default_reference':  f'COD Payment for {self.name}',
                'default_seller_id': self.seller_id.id,

            },
            'target': 'new',
        }

    def action_view_cod_payment_transfers(self):
        self.ensure_one()
        if self.cod_payment_transfer_ids:
            return {
                'name': 'COD Account Transfers',
                'type': 'ir.actions.act_window',
                'res_model': 'logistics.account.transfer',
                'view_mode': 'list,form',
                'domain': [('id', 'in', self.cod_payment_transfer_ids.ids)],
                'context': {"create": 0, "no_create": 1},
            }

    def action_create_payment_cod_from_portal(self, payment_method: str):
        for rec in self:
            if rec.order_payment_type == 'cod' and rec.cod_balance_amount > 0:
                if payment_method == 'upi':
                    from_account_id = self.env['logistics.account'].search([('account_type', '=', 'cod_customer'), ('name', 'ilike', 'upi')], limit=1)
                elif payment_method == 'cash':
                    from_account_id = self.env['logistics.account'].search([('account_type', '=', 'cod_customer'), ('name', 'ilike', 'cash')], limit=1)

                cod_payment_wizard = self.env['logistics.cod.payment.wizard'].create({
                    'from_account_id': from_account_id.id,
                    'to_account_id': from_account_id.id, #dummy to_account
                    'shipment_id': rec.id,
                    'amount': self.cod_balance_amount,
                    'reference':  f'COD Payment for {self.name}',
                    'seller_id': self.seller_id.id,
                })
                cod_payment_wizard.from_account_id = from_account_id.id
                # Compute new to_account from from_account
                cod_payment_wizard._compute_to_account()
                cod_payment_wizard.action_create_transfer()