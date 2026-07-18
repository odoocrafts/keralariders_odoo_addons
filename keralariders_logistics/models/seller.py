from odoo import models, fields, api

districts = [
    ('Alappuzha', 'Alappuzha'),
    ('Ernakulam', 'Ernakulam'),
    ('Idukki', 'Idukki'),
    ('Kannur', 'Kannur'),
    ('Kasaragod', 'Kasaragod'),
    ('Kollam', 'Kollam'),
    ('Kottayam', 'Kottayam'),
    ('Kozhikode', 'Kozhikode'),
    ('Malappuram', 'Malappuram'),
    ('Palakkad', 'Palakkad'),   
    ('Pathanamthitta', 'Pathanamthitta'),
    ('Thiruvananthapuram', 'Thiruvananthapuram'),
    ('Thrissur', 'Thrissur'),
    ('Wayanad', 'Wayanad'),
]

class Seller(models.Model):
    _name = 'logistics.seller'
    _description = 'Seller/Vendor'

    partner_id = fields.Many2one('res.partner', string='Partner')
    name = fields.Char(string='Seller Name', related='partner_id.name', required=True, store=True, readonly=False)
    email = fields.Char(string='Email', related='partner_id.email', store=True, readonly=False)
    phone = fields.Char(string='Phone', related='partner_id.phone', store=True, readonly=False)
    street = fields.Char(string='Address', related='partner_id.street', store=True, readonly=False)
    street2 = fields.Char(string='Address 2', related='partner_id.street2', store=True, readonly=False)
    city = fields.Char(string='City', related='partner_id.city', store=True, readonly=False,)
    state_id = fields.Many2one('res.country.state', string='State', related='partner_id.state_id', store=True, readonly=False, default=lambda self: self.env.company.state_id.id, domain="[('country_id', '=', country_id)]")
    country_id = fields.Many2one('res.country', string='Country', related='partner_id.country_id', store=True, readonly=False,  default=lambda self: self.env.company.partner_id.country_id.id)
    zip = fields.Char(string='ZIP', related='partner_id.zip', store=True, readonly=False)
    tax_id = fields.Char(string='GSTIN', related='partner_id.vat', store=True, readonly=False)
    district = fields.Selection(selection=districts, string='District')

    # @api.onchange('state_id')
    # def _onchange_state_id(self):
    #     if self.state_id:
    #         self.country_id = self.state_id.country_id
    #     else:
    #         self.country_id = False

    # @api.onchange('country_id')
    # def _onchange_country_id(self):
    #     if self.country_id and self.country_id != self.state_id.country_id:
    #         self.state_id = False

    @api.model_create_multi
    def create(self, vals_list):
        recs = super(Seller, self).create(vals_list)
        for rec in recs:
            # Create a new partner record for the seller
            partner_vals = {
                'name': rec.name,
                'email': rec.email,
                'phone': rec.phone,
                'street': rec.street,
                'street2': rec.street2,
                'city': rec.city,
                'state_id': rec.state_id.id,
                'country_id': rec.country_id.id,
                'zip': rec.zip,
                'vat': rec.tax_id,
            }
            rec.partner_id = self.env['res.partner'].create(partner_vals).id
            # Create a new Wallet record for the seller
            wallet_vals = {
                'seller_id': rec.id,
            }
            self.env['logistics.wallet'].create(wallet_vals)
        return recs
    
    wallet_ids = fields.One2many('logistics.wallet', 'seller_id', string='Wallets')

    def unlink(self):
        partners = self.mapped('partner_id')
        res =  super(Seller, self).unlink()
        partners.unlink()
        return res
    
    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)
    wallet_ids = fields.One2many('logistics.wallet', 'seller_id', string='Wallets')
    wallet_count = fields.Integer(string='Wallet Count', compute='_compute_wallet_count')
    total_wallet_balance = fields.Float(string='Total Wallet Balance', compute='_compute_total_wallet_balance')
    @api.depends('wallet_ids')
    def _compute_total_wallet_balance(self):
        for seller in self:
            total_balance = sum(wallet.balance for wallet in seller.wallet_ids)
            seller.total_wallet_balance = total_balance
    @api.depends('wallet_ids')
    def _compute_wallet_count(self):
        for seller in self:
            seller.wallet_count = len(seller.wallet_ids)

    def action_view_wallets(self):
        self.ensure_one()
        return {
            'name': 'Wallets',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.wallet',
            'view_mode': 'list,form',
            'domain': [('seller_id', '=', self.id)],
            'context': {'default_seller_id': self.id},
        }