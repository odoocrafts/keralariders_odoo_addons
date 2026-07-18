from odoo import models, fields, api, _

class Shipment(models.Model):
    _name = 'logistics.shipment'
    _description = 'Shipment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    # _order = 'create_date desc'

    name = fields.Char(string='Shipment Reference (AWB)', required=True, copy=False, readonly=True, index=True, default=lambda self: _('New'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('logistics.shipment') or _('New')
        return super(Shipment, self).create(vals_list)
    seller_id = fields.Many2one('logistics.seller', string='Seller', required=True)
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
    shipping_from_zip = fields.Char(string='Shipping From Pincode', compute='_compute_shippping_from', store=True, readonly=False)
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
    shipping_to_zip = fields.Char(string='Shipping To Pincode')
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
    actual_delivery_date = fields.Date(string='Actual Delivery Date')

    order_payment_type = fields.Selection([('prepaid', 'Prepaid'), ('cod', 'Cash on Delivery')], string='Order Payment Type', required=True, default='prepaid')