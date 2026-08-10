from odoo import models, fields, api, _


class ShipmentEvent(models.Model):
    _name = 'logistics.shipment.event'
    _description = 'Shipment Custody Event'
    _order = 'event_time desc, id desc'

    shipment_id = fields.Many2one(
        'logistics.shipment',
        string='Shipment',
        required=True,
        ondelete='cascade',
        index=True,
    )
    event_type = fields.Selection([
        ('pickup_scan', 'Pickup Scan'),
        ('dropped_at_hub', 'Dropped at Hub'),
        ('hub_receive', 'Hub Receive'),
        ('hub_dispatch', 'Hub Dispatch'),
        ('depart_hub', 'Departed Hub'),
        ('de_self_assign', 'DE Self-Assign'),
        ('leg_assign', 'Leg Assign'),
        ('de_accept_assignment', 'DE Accepted Assignment'),
        ('de_reject_assignment', 'DE Rejected Assignment'),
        ('central_pass_through', 'Passed via Thrissur Hub'),
        ('skip_hub_local', 'Local Delivery (Skip Hub)'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered'),
        ('return_requested', 'Return Requested'),
        ('returned', 'Returned to Sender'),
        ('status_override', 'Status Override'),
        ('note', 'Note'),
    ], string='Event Type', required=True, index=True)

    event_time = fields.Datetime(string='Timestamp', default=fields.Datetime.now, required=True, index=True)
    actor_user_id = fields.Many2one('res.users', string='Actor User', default=lambda self: self.env.user)
    actor_de_id = fields.Many2one('logistics.delivery.executive', string='Actor DE')
    hub_id = fields.Many2one('logistics.hub', string='Hub')
    leg_id = fields.Many2one('logistics.shipment.estimated.route', string='Route Leg')

    from_custodian_type = fields.Selection([
        ('seller', 'Seller'),
        ('de', 'Delivery Executive'),
        ('hub', 'Hub'),
        ('customer', 'Customer'),
    ], string='From Custodian')
    to_custodian_type = fields.Selection([
        ('seller', 'Seller'),
        ('de', 'Delivery Executive'),
        ('hub', 'Hub'),
        ('customer', 'Customer'),
    ], string='To Custodian')

    scanned_code = fields.Char(string='Scanned Code')
    note = fields.Text(string='Note')
    name = fields.Char(string='Summary', compute='_compute_name', store=True)

    @api.depends('event_type', 'shipment_id', 'hub_id', 'event_time')
    def _compute_name(self):
        type_labels = dict(self._fields['event_type'].selection)
        for rec in self:
            label = type_labels.get(rec.event_type, rec.event_type or '')
            hub = rec.hub_id.name if rec.hub_id else ''
            awb = rec.shipment_id.name if rec.shipment_id else ''
            rec.name = f"{label} — {awb}" + (f" @ {hub}" if hub else '')

    def get_timeline_detail(self, public=False):
        """Short human-readable detail line for public/portal tracking.

        When public=True, omit DE/hub-manager actor names and sanitize notes
        (billing jargon, legacy "by Name" fragments) for /track.
        """
        self.ensure_one()
        parts = []
        if self.hub_id:
            parts.append(self.hub_id.name)
        if self.actor_de_id and not public:
            parts.append(_("by %s") % self.actor_de_id.name)
        if self.note:
            note = self.note.strip()
            if public and self.shipment_id:
                note = self.shipment_id._sanitize_public_timeline_detail(note)
            if note:
                parts.append(note)
        return ' · '.join(parts) if parts else False
