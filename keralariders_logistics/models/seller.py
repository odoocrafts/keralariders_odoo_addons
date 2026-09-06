from odoo import models, fields, api, _
from odoo.exceptions import AccessError

FULFILMENT_METHODS = [
    ('indiapost', 'India Post (Speed Post)'),
    ('own_network', 'KeralaXpress Hub Network'),
]

# Only a Logistics Administrator may decide how a seller's parcels travel.
FULFILMENT_ADMIN_GROUP = 'keralariders_logistics.group_logistics_admin'


class Seller(models.Model):
    _name = 'logistics.seller'
    _description = 'Seller/Vendor'
    _inherit = ['mail.thread', 'mail.activity.mixin','avatar.mixin']

    # Update image of partner_id too
    @api.onchange('image_1920')
    def _onchange_image_1920(self):
        self.partner_id.image_1920 = self.image_1920

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
        # Seller self-signup runs as sudo, so the same guard has to cover
        # create; otherwise a crafted signup form could pick its own carrier.
        for vals in vals_list:
            self._ip_check_fulfilment_method_write(vals)
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

            # Seller settlement ledger account for COD clearance
            if not rec.seller_account_id:
                rec.seller_account_id = self.env['logistics.account'].sudo().create({
                    'name': f'{rec.name} Seller',
                    'account_type': 'seller',
                    'seller_id': rec.id,
                    'reference': 'Auto-created seller COD settlement account',
                }).id
        return recs

    def action_clear_cod_to_seller(self):
        """Admin helper: clear banked COD payments from company → this seller."""
        self.ensure_one()
        transfer = self.env['logistics.account.transfer'].action_create_cod_clearance(seller=self)
        return {
            'type': 'ir.actions.act_window',
            'name': _('COD Clearance'),
            'res_model': 'logistics.account.transfer',
            'res_id': transfer.id,
            'view_mode': 'form',
            'target': 'current',
        }

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
                'group_ids': [(4, portal_group.id)]
            })
            user.action_reset_password()
            message = "Portal access granted and invitation email sent!"
        else:
            if portal_group not in user.group_ids:
                user.sudo().write({'group_ids': [(4, portal_group.id)]})
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
    
    pan_number = fields.Char(string='PAN Number', tracking=True)
    fssai_number = fields.Char(string='FSSAI Number', tracking=True)

    # Bank details for COD settlement (match PAN/FSSAI style — seller-local Char fields)
    bank_account_name = fields.Char(string='Bank Account Name', tracking=True)
    bank_account_number = fields.Char(string='Bank Account Number', tracking=True)
    bank_ifsc = fields.Char(string='IFSC Code', tracking=True)
    bank_name = fields.Char(string='Bank Name', tracking=True)
    bank_branch = fields.Char(string='Bank Branch', tracking=True)

    seller_account_id = fields.Many2one(
        'logistics.account',
        string='Seller COD Account',
        help='Ledger account used for Company → Seller COD clearance.',
    )

    delivery_package_id = fields.Many2one('logistics.delivery.package', string="Delivery Package", help="Special pricing package for this seller. Leave empty to use the default rates.")

    # -------------------------------------------------------------------------
    # Fulfilment method
    #
    # KeralaXpress ships through India Post today and plans to move onto its own
    # hub network in about a year, so both paths stay live and this switch picks
    # between them per seller. It is an internal commercial decision: sellers
    # must not be able to change it, which is enforced three ways —
    #   * field-level ``groups`` removes it from the ORM for everyone else, so
    #     even a hand-crafted RPC write cannot reach it;
    #   * :meth:`write` re-checks the group for sudo callers;
    #   * the portal never renders it as an input.
    # -------------------------------------------------------------------------
    fulfilment_method = fields.Selection(
        FULFILMENT_METHODS,
        string='Fulfilment Method',
        default='indiapost',
        required=True,
        tracking=True,
        groups=FULFILMENT_ADMIN_GROUP,
        help="Carrier used for this seller's shipments. India Post books "
             "through the Department of Posts bulk customer API; the hub "
             "network uses KeralaXpress delivery executives and hubs. "
             "Administrators only.",
    )

    def _ip_can_set_fulfilment_method(self):
        """Whether the current user may set the fulfilment method.

        Checks the *real* user rather than the superuser flag, so a
        ``sudo()`` call made while serving a portal request is still refused.
        Trusted server code opts in through the context key, which is the same
        convention ``logistics.shipment`` already uses to protect ``state``.
        """
        if self.env.context.get('allow_fulfilment_method_write'):
            return True
        return self.env.user.has_group(FULFILMENT_ADMIN_GROUP)

    def _ip_check_fulfilment_method_write(self, vals):
        if 'fulfilment_method' in vals and not self._ip_can_set_fulfilment_method():
            raise AccessError(_(
                "Only a Logistics Administrator can change a seller's "
                "fulfilment method. Please contact KeralaXpress support."
            ))

    def write(self, vals):
        # Field-level groups already hide the field from non-admins, but
        # sudo() bypasses them, so the value is guarded here as well.
        self._ip_check_fulfilment_method_write(vals)
        return super().write(vals)

    def _ip_fulfilment_method(self):
        """The seller's method, readable regardless of the caller's group."""
        self.ensure_one()
        return self.sudo().fulfilment_method or 'indiapost'

    def _ip_uses_indiapost(self):
        return self._ip_fulfilment_method() == 'indiapost'

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

    order_ids = fields.One2many('logistics.order', 'seller_id', string="Orders")
    orders_count = fields.Integer(compute="_compute_orders_shipment_count")
    shipment_ids = fields.One2many('logistics.shipment', 'seller_id', string="Shipments")
    shipments_count = fields.Integer(compute="_compute_orders_shipment_count")

    def _compute_orders_shipment_count(self):
        for rec in self:
            rec.orders_count = len(rec.order_ids)
            rec.shipments_count = len(rec.shipment_ids)

    def action_view_orders(self):
        self.ensure_one()
        return {
            'name': 'Orders',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.order',
            'view_mode': 'kanban,list,form',
            'domain': [('seller_id', '=', self.id)],
            'context': {'default_seller_id': self.id},
        }

    def action_view_shipments(self):
        self.ensure_one()
        return {
            'name': 'Shipments',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.shipment',
            'view_mode': 'list,form',
            'domain': [('seller_id', '=', self.id)],
            'context': {'default_seller_id': self.id},
        }

    
    user_id = fields.Many2one('res.users', compute="_compute_user_id")

    def _compute_user_id(self):
        for rec in self:
            rec.user_id = self.env['res.users'].search([('partner_id','=', self.partner_id.id)], limit=1).id

    def action_update_password(self):
        return {
            'type': 'ir.actions.act_window',
            'target': 'new',
            'res_model': 'change.password.wizard',
            'view_mode': 'form',
            'context': {
                "active_model": 'res.users',
                'active_ids': self.user_id.ids
            }
        }