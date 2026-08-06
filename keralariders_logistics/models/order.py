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
        for order in self:
            if not order.shipment_ids:
                order.state = 'draft'
                continue
                
            states = order.shipment_ids.mapped('state')
            if any(s in ('delivered', 'return_requested', 'return_picked', 'returned') for s in states):
                order.state = 'partial'

            if all(s == 'delivered' for s in states):
                order.state = 'delivered'
            elif all(s == 'cancelled' for s in states):
                order.state = 'cancelled'
            elif all(s == 'picked' for s in states):
                order.state = 'picked'
            elif all(s == 'pickup_requested' for s in states):
                order.state = 'pickup_requested'
            # else:
            #     order.state = 'draft'

    def action_request_pickup(self):
        for order in self:
            if order.state != 'draft':
                raise UserError("Only draft orders can request pickup.")
                
            for shipment in order.shipment_ids:
                shipment.action_add_wallet_transaction()
                # Ensure pickup DE / pickup leg assignment (wallet unchanged).
                shipment._auto_assign_pickup_executive()
            
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
        return self.env.ref('keralariders_logistics.action_report_shipment').report_action(self.shipment_ids)