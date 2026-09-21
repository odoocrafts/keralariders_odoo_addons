from odoo import models, fields, api, _
from odoo.exceptions import UserError

class Order(models.Model):
    _name = 'logistics.order'
    _description = 'Order'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'name desc, write_date desc'
    
    name = fields.Char(string='Order Reference', required=True, copy=False, readonly=True, index=True, default=lambda self: _('New'))
    
    seller_id = fields.Many2one('logistics.seller', string='Seller', required=True)
    order_date = fields.Date(string='Order Date', required=True, default=fields.Date.context_today)
    pickup_date = fields.Date(string='Pickup Date', required=True, default=fields.Date.context_today)

    shipment_ids = fields.One2many('logistics.shipment', 'order_id', string='Shipments')
    
    shipment_count = fields.Integer(string='Number of Shipments', compute='_compute_shipment_details')
    delivered_shipment_count = fields.Integer(string="Number of Delivered Shipments")
    total_charges = fields.Monetary(string='Total Delivery Charges', currency_field='currency_id', compute='_compute_shipment_details')
    
    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)
    company_id = fields.Many2one('res.company', string='Company', required=True, default=lambda self: self.env.company)
    
    state = fields.Selection([
        ('draft', 'Draft'),
        ('pickup_requested', 'Pickup Requested'),
        ('picked', 'Picked Up'),
        ('partial', 'Partial Delivery'),
        ('delivered', 'Fully Delivered'),
        ('cancelled', 'Cancelled')
    ], string='Status', default='draft', compute='_compute_state', store=True, tracking=True)

    # Shipment states that mean the parcel has already left the seller.
    # Own-network pickup writes ``picked``; India Post often jumps to
    # ``in_transit`` / ``out_for_delivery`` without that intermediate.
    _PAST_PICKUP_STATES = frozenset({
        'picked',
        'in_transit',
        'at_source_hub',
        'at_central_hub',
        'at_destination_hub',
        'out_for_delivery',
        'delivery_failed',
        'delivered',
        'return_requested',
        'return_picked',
        'returned',
    })
    _PARTIAL_DELIVERY_STATES = frozenset({
        'delivered', 'return_requested', 'return_picked', 'returned',
    })

    @api.model
    def _domain_not_draft(self):
        """Booked orders only: draft is unfinished and omitted from seller badges."""
        return [('state', '!=', 'draft')]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].sudo().next_by_code('logistics.order') or _('New')
        return super(Order, self).create(vals_list)

    @api.depends('shipment_ids', 'shipment_ids.delivery_charges_total')
    def _compute_shipment_details(self):
        for order in self:
            order.shipment_count = len(order.shipment_ids)
            order.delivered_shipment_count = len(order.shipment_ids.filtered(lambda rec: rec.state in ('delivered', 'return_requested', 'return_picked', 'returned')))
            order.total_charges = sum(order.shipment_ids.mapped('delivery_charges_total'))

    @api.depends('shipment_ids.state')
    def _compute_state(self):
        """Map shipment lifecycle onto the order status bar.

        Fully Delivered only when every non-cancelled shipment is delivered.
        One out-for-delivery / in-transit shipment must not jump the order
        there — it only proves pickup already happened (Picked Up).
        """
        for order in self:
            if not order.shipment_ids:
                order.state = 'draft'
                continue

            states = order.shipment_ids.mapped('state')
            active = [s for s in states if s != 'cancelled']
            if not active:
                order.state = 'cancelled'
            elif all(s == 'delivered' for s in active):
                order.state = 'delivered'
            elif any(s in order._PARTIAL_DELIVERY_STATES for s in active):
                order.state = 'partial'
            elif any(s in order._PAST_PICKUP_STATES for s in active):
                order.state = 'picked'
            elif all(s == 'pickup_requested' for s in active):
                order.state = 'pickup_requested'
            else:
                order.state = 'draft'

    def _advance_picked_up_from_shipments(self):
        """Recompute these orders from current shipment states.

        ``_compute_state`` already runs when a shipment state is written.
        Tracking polls / webhooks / Sync Now can re-apply out-for-delivery
        without changing the shipment, so the stored compute does not rerun.
        """
        if self:
            self._compute_state()
        return self

    @api.model
    def _advance_stuck_pickup_requested_orders(self):
        """Catch pickup_requested orders whose parcels already left the seller.

        Stored compute is not recomputed on upgrade, so records such as
        ORD26090072 (shipment out for delivery, order still Pickup Requested)
        flip on the next tracking cron / Sync Now. Safe and idempotent.
        """
        stuck = self.search([
            ('state', '=', 'pickup_requested'),
            ('shipment_ids.state', 'in', list(self._PAST_PICKUP_STATES)),
        ])
        if stuck:
            stuck._compute_state()
        return stuck

    def action_request_pickup(self):
        for order in self:
            self.env.cr.execute(
                'SELECT id FROM logistics_order WHERE id = %s FOR UPDATE',
                (order.id,),
            )
            order.invalidate_recordset(['state'])
            if order.state != 'draft':
                raise UserError("Only draft orders can request pickup.")
                
            for shipment in order.shipment_ids:
                shipment.action_add_wallet_transaction()
                # Ensure pickup DE / pickup leg assignment (wallet unchanged).
                # India Post collects from the seller itself, so those get no
                # KeralaXpress pickup executive and no pickup leg assignment.
                shipment._needs_keralaxpress_pickup()._auto_assign_pickup_executive()
            
            # Update all shipments
            order.shipment_ids.with_context(allow_shipment_state_write=True).write({
                'state': 'pickup_requested',
                'pickup_requested_on': fields.Datetime.now()
            })

    def action_mark_picked_up(self):
        for order in self:
            if order.state != 'pickup_requested':
                raise UserError("Only order requested for Pickup can be marked as Picked Up.")
            order.shipment_ids.action_mark_picked()
            order.state = 'picked'

    def action_reset_draft(self):
        for order in self:
            order.shipment_ids.with_context(allow_shipment_state_write=True).write({
                'state': 'order_added',
            })
            order.state = 'draft'

    def action_cancel_order(self):
        for order in self:
            for shipment in order.shipment_ids:
                shipment.delete_wallet_transaction()
            order.shipment_ids.with_context(allow_shipment_state_write=True).write({
                'state': 'cancelled'
            })
            order.state = 'cancelled'

    def action_print_awb_delivery_slips(self):
        shipments = self.mapped('shipment_ids')
        if not shipments:
            raise UserError(_('No shipments to print.'))
        return self.env['logistics.awb.print.wizard']._action_open(shipments)

    def portal_awb_printable(self):
        """Seller portal may print AWBs only after pickup has been requested."""
        if not self:
            return False
        self.ensure_one()
        if self.state == 'draft':
            return False
        return bool(self.shipment_ids) and self.shipment_ids.portal_awb_printable()