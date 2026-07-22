from odoo import models, fields, api

class Seller(models.Model):
    _name = 'logistics.seller'
    _description = 'Seller/Vendor'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    partner_id = fields.Many2one('res.partner', string='Partner')
    name = fields.Char(string='Seller Name', related='partner_id.name', required=True, store=True, readonly=False)
    email = fields.Char(string='Email', related='partner_id.email', store=True, readonly=False)
    phone = fields.Char(string='Phone', related='partner_id.phone', store=True, readonly=False)
    street = fields.Char(string='Address', related='partner_id.street', store=True, readonly=False)
    street2 = fields.Char(string='Address 2', related='partner_id.street2', store=True, readonly=False)
    city = fields.Char(string='City', related='partner_id.city', store=True, readonly=False,)
    state_id = fields.Many2one('res.country.state', string='State', related='partner_id.state_id', store=True, readonly=False, default=lambda self: self.env.company.state_id.id, domain="[('country_id', '=', country_id)]")
    country_id = fields.Many2one('res.country', string='Country', related='partner_id.country_id', store=True, readonly=False,  default=lambda self: self.env.company.partner_id.country_id.id)
    zip = fields.Char(string='Pincode', related='partner_id.zip', store=True, readonly=False)
    tax_id = fields.Char(string='GSTIN', related='partner_id.vat', store=True, readonly=False)
    district_id = fields.Many2one('logistics.district', string='District')

    @api.onchange('zip')
    def _onchange_zip(self):
        if self.zip:
            pincode_info = self.env['logistics.district'].get_district_from_pincode(self.zip)
            self.district_id = pincode_info['district_id'].id if pincode_info['district_id'] else False
            self.state_id = pincode_info['district_id'].state_id.id if pincode_info['district_id'] else False

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
            # Create a new partner record for the seller if not already provided
            if not rec.partner_id:
                partner_vals = {
                    'name': rec.name,
                    'email': rec.email,
                    'phone': rec.phone,
                    'street': rec.street,
                    'street2': rec.street2,
                    'city': rec.city,
                    'state_id': rec.state_id.id if rec.state_id else False,
                    'country_id': rec.country_id.id if rec.country_id else False,
                    'zip': rec.zip,
                    'vat': rec.tax_id,
                }
                rec.partner_id = self.env['res.partner'].create(partner_vals).id
            
            # Create a new Wallet record for the seller
            wallet_vals = {
                'name': f"{rec.name} - Wallet",
                'seller_id': rec.id,
            }
            self.env['logistics.wallet'].create(wallet_vals)
        return recs

    def action_grant_portal_access(self):
        self.ensure_one()
        from odoo.exceptions import UserError
        
        if not self.partner_id:
            raise UserError("Seller must have a related partner to grant portal access.")
            
        if not self.partner_id.email:
            raise UserError("Seller must have an email address to grant portal access.")
            
        portal_group = self.env.ref('base.group_portal')
        user = self.env['res.users'].sudo().search([('partner_id', '=', self.partner_id.id)], limit=1)
        
        if not user:
            # Check if login already exists
            if self.env['res.users'].sudo().search([('login', '=', self.partner_id.email)]):
                raise UserError("A user with this email already exists.")
                
            user = self.env['res.users'].sudo().create({
                'name': self.partner_id.name,
                'login': self.partner_id.email,
                'partner_id': self.partner_id.id,
                'groups_id': [(4, portal_group.id)]
            })
            user.action_reset_password()
            message = "Portal access granted and invitation email sent!"
        else:
            if portal_group not in user.groups_id:
                user.sudo().write({'groups_id': [(4, portal_group.id)]})
                message = "Portal access granted!"
            else:
                message = "Seller already has portal access."
                
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Portal Access',
                'message': message,
                'type': 'success',
                'sticky': False,
            }
        }
    
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
    
    wallet_recharge_request_ids = fields.One2many('logistics.wallet.recharge.request', 'seller_id', string="Recharge Requests")
    wallet_recharge_request_count = fields.Integer(compute="_compute_wallet_recharge_request_count")
    def _compute_wallet_recharge_request_count(self):
        for rec in self:
            rec.wallet_recharge_request_count = len(rec.wallet_recharge_request_ids)
            
    def action_view_recharge_requests(self):
        self.ensure_one()
        return {
            'name': 'Wallet Recharge Requests',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.wallet.recharge.request',
            'view_mode': 'list,form',
            'domain': [('seller_id', '=', self.id)],
            'context': {'default_seller_id': self.id},
        }