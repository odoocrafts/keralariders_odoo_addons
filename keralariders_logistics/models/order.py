from odoo import models, fields, api, _
from odoo.exceptions import UserError

class Order(models.Model):
    _name = 'logistics.order'
    _description = 'Order'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(string='Order Reference', required=True, copy=False, readonly=True, index=True, default=lambda self: _('New'))
    
    seller_id = fields.Many2one('logistics.seller', string='Seller', required=True)
    order_date = fields.Date(string='Order Date', required=True, default=fields.Date.context_today)
    pickup_date = fields.Date(string='Pickup Date', required=True, default=fields.Date.context_today)

    shipment_ids = fields.One2many('logistics.shipment', 'order_id', string='Shipments')
    
    shipment_count = fields.Integer(string='Number of Shipments', compute='_compute_shipment_details', store=True)
    total_charges = fields.Monetary(string='Total Delivery Charges', currency_field='currency_id', compute='_compute_shipment_details', store=True)
    
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
            order.total_charges = sum(order.shipment_ids.mapped('delivery_charges_total'))

    @api.depends('shipment_ids.state')
    def _compute_state(self):
        for order in self:
            if not order.shipment_ids:
                order.state = 'draft'
                continue
                
            states = order.shipment_ids.mapped('state')
            if all(s == 'delivered' for s in states):
                order.state = 'delivered'
            elif any(s == 'delivered' for s in states):
                order.state = 'partial'
            elif all(s == 'cancelled' or s == 'cancel' for s in states):
                order.state = 'cancelled'
            elif any(s == 'picked' for s in states):
                order.state = 'picked'
            elif any(s == 'pickup_requested' for s in states):
                order.state = 'pickup_requested'
            else:
                order.state = 'draft'

    def action_request_pickup(self):
        for order in self:
            if order.state != 'draft':
                raise UserError("Only draft orders can request pickup.")
                
            wallet = order.seller_id.wallet_ids[0] if order.seller_id.wallet_ids else False
            if not wallet:
                raise UserError("No wallet found for this seller.")
                
            if wallet.balance < order.total_charges:
                raise UserError(f"Insufficient wallet balance. Charge is {order.total_charges}, balance is {wallet.balance}.")
                
            # Create a single wallet transaction for the order
            self.env['logistics.wallet.transaction'].sudo().create({
                'wallet_id': wallet.id,
                'amount': -order.total_charges,
                'transaction_date': fields.Date.context_today(self),
                'order_id': order.id,
                'reference': f"Charge for Order {order.name}",
            })
            
            # Update all shipments
            order.shipment_ids.write({
                'state': 'pickup_requested',
                'pickup_requested_on': fields.Datetime.now()
            })
