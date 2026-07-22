from odoo import models, fields, api, _
from odoo.exceptions import UserError
import uuid

delivery_states = [
    ('order_added', 'Order Added'),
    ('pickup_requested', 'Pickup Requested'),
    ('picked', 'Picked'),
    ('in_transit', 'In Transit'),
    ('delivered', 'Delivered'),
    ('cancelled', 'Cancelled'),
    ('return_requested', 'Return Requested'),
    ('return_picked', 'Return Picked'),
    ('returned', 'Returned'),
    ('cancel', 'Cancelled'),
]

class Shipment(models.Model):
    _name = 'logistics.shipment'
    _description = 'Shipment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    # _order = 'create_date desc'

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
                vals['name'] = self.env['ir.sequence'].next_by_code('logistics.shipment') or _('New')
        return super(Shipment, self).create(vals_list)

    company_id = fields.Many2one('res.company', string='Company', required=True, default=lambda self: self.env.company)

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

    order_payment_type = fields.Selection([('prepaid', 'Prepaid'), ('cod', 'Cash on Delivery'), ('na', 'Not Applicable')], string='Order Payment Type', required=True, default='prepaid')

    delivery_charges_subtotal = fields.Monetary(string='Delivery Charges (Subtotal)', currency_field='currency_id', compute='_compute_delivery_charges', store=True, readonly=False)
    @api.depends('total_weight', 'shipping_from_district_id', 'shipping_to_district_id', 'tax_percentage')
    def _compute_delivery_charges(self):
        for record in self:
            if record.total_weight:
                same_district = (record.shipping_from_district_id == record.shipping_to_district_id)
                record.delivery_charges_subtotal = self.env['logistics.delivery.charges'].sudo().calculate_delivery_charge(record.total_weight, same_district)
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

    wallet_transaction_id = fields.Many2one("logistics.wallet.transaction", string="Wallet Transaction")

    def action_add_wallet_transaction(self):
        if not self.seller_id:
            raise UserError(f'Seller must be set before adding Wallet Transaction!')
        if not self.seller_id.wallet_ids:
            raise UserError(f'No Wallets found for this Seller!')
        if not self.wallet_transaction_id:
            wallet = self.seller_id.wallet_ids[0]
            # Check wallet balance
            if wallet.balance < self.delivery_charges_total:
                raise UserError(f'Insufficient balance available in your Wallet. Please recharge before proceeding')
            
            self.wallet_transaction_id = self.env['logistics.wallet.transaction'].create({
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