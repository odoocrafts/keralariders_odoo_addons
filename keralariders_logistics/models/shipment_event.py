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
        ('central_pass_through', 'Central Pass-Through'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered'),
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
