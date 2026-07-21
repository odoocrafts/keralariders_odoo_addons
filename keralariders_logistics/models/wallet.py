from odoo import models, fields, api, _
from odoo.exceptions import UserError

class Wallet(models.Model):
    _name = 'logistics.wallet'
    _description = 'Wallet'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string='Wallet Name', required=True, compute='_compute_wallet_name', store=True, readonly=False)
    
    @api.depends('seller_id')
    def _compute_wallet_name(self):
        for wallet in self:
            wallet.name = f"{wallet.seller_id.name} - Wallet" if wallet.seller_id else ''
    seller_id = fields.Many2one('logistics.seller', string='Seller', required=True)
    transaction_ids = fields.One2many('logistics.wallet.transaction', 'wallet_id', string='Transactions')
    balance = fields.Monetary(string='Balance', compute="_compute_balance", currency_field='currency_id')

    def _compute_balance(self):
        for wallet in self:
            wallet.balance = sum(transaction.amount for transaction in wallet.transaction_ids if transaction.transaction_type == 'credit') - sum(transaction.amount for transaction in wallet.transaction_ids if transaction.transaction_type == 'debit')

    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)

    def action_view_transactions(self):
        self.ensure_one()
        return {
            'name': 'Wallet Transactions',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.wallet.transaction',
            'view_mode': 'list,form',
            'domain': [('wallet_id', '=', self.id)],
            'context': {'default_wallet_id': self.id},
        }
    
    total_credit = fields.Float(string='Total Credit', compute='_compute_total_credit')
    total_debit = fields.Float(string='Total Debit', compute='_compute_total_debit')
    def _compute_total_credit(self):
        for wallet in self:
            total_credit = sum(transaction.amount for transaction in wallet.transaction_ids if transaction.transaction_type == 'credit')
            wallet.total_credit = total_credit

    def _compute_total_debit(self):
        for wallet in self:
            total_debit = sum(transaction.amount for transaction in wallet.transaction_ids if transaction.transaction_type == 'debit')
            wallet.total_debit = total_debit

        
    wallet_recharge_request_ids = fields.One2many('logistics.wallet.recharge.request', 'wallet_id', string="Recharge Requests")
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
            'domain': [('seller_id', '=', self.seller_id.id), ('wallet_id', '=', self.id)],
            'context': {'default_seller_id': self.seller_id.id, 'default_wallet_id': self.id},
        }
    
class WalletTransaction(models.Model):
    _name = 'logistics.wallet.transaction'
    _description = 'Wallet Transaction'

    wallet_id = fields.Many2one('logistics.wallet', string='Wallet', required=True)
    transaction_type = fields.Selection([('credit', 'Credit'), ('debit', 'Debit')], string='Transaction Type', default='credit', compute="_compute_transaction_type", store=True)
    @api.depends('amount')
    def _compute_transaction_type(self):
        for transaction in self:
            transaction.transaction_type = 'credit' if transaction.amount >= 0 else 'debit'
    amount = fields.Monetary(string='Amount', required=True, currency_field='currency_id')
    transaction_date = fields.Date(string='Transaction Date', default=fields.Date.context_today, required=True)
    description = fields.Text(string='Description')
    reference = fields.Text(string='Reference')
    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)
    shipment_id = fields.Many2one('logistics.shipment', string="Related Shipment", ondelete="cascade")
    recharge_request_id = fields.Many2one('logistics.wallet.recharge.request', string="Recharge Request", ondelete="cascade")
    
class WalletRechargeRequest(models.Model):
    _name = "logistics.wallet.recharge.request"
    _description = "Wallet Recharge Request"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    name = fields.Char(string="Reference", readonly=1, store=True, default=lambda self: _('New'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('logistics.wallet.recharge.request') or _('New')
        return super(WalletRechargeRequest, self).create(vals_list)
    
    request_date = fields.Datetime(string="Request Date", default=fields.Datetime.now)
    seller_id = fields.Many2one('logistics.seller', string="Seller", required=True)
    wallet_id = fields.Many2one('logistics.wallet', string="Wallet", required=True, domain="[('seller_id', '=', seller_id)]", compute="_compute_wallet_id", store=True, readonly=False)
    @api.depends('seller_id')
    def _compute_wallet_id(self):
        for rec in self:
            if rec.seller_id and rec.seller_id.wallet_ids:
                rec.wallet_id = rec.seller_id.wallet_ids[0].id
            else:
                rec.wallet_id = False
    requested_amount = fields.Monetary(string="Amount Requested")
    recharged_amount = fields.Monetary(string="Amount Recharged", compute="_compute_recharged_amount", store=True, readonly=False)
    
    @api.depends('requested_amount')
    def _compute_recharged_amount(self):
        for rec in self:
            rec.recharged_amount = rec.requested_amount

    currency_id = fields.Many2one('res.currency', string="Currency", default=lambda self: self.env.company.currency_id.id)
    company_id = fields.Many2one('res.company', string="Company", default=lambda self: self.env.company.id)
    approved_date = fields.Datetime(string="Approved On")
    approved_by = fields.Many2one('res.users', string="Approved By")
    remarks = fields.Text(string="Remarks")
    state = fields.Selection([('pending_approval', 'Pending Approval'), ('approved', 'Approved'), ('cancelled', 'Cancelled')], string="Status", default='pending_approval')
    wallet_transaction_id = fields.Many2one('logistics.wallet.transaction', string="Wallet Transaction")

    def action_approve_request(self):
        if self.recharged_amount <= 0:
            raise UserError(f'Recharge amount must be greater than 0.')
        if not self.wallet_transaction_id:
            self.approved_by = self.env.user.id
            self.approved_date = fields.Datetime.now()
            self.wallet_transaction_id = self.env['logistics.wallet.transaction'].create({
                'wallet_id': self.wallet_id.id,
                'amount': self.recharged_amount,
                'transaction_date': fields.Date.context_today(self),
                'reference': f'Recharge - {self.display_name}',
            }).id
            self.state = 'approved'

    def action_cancel(self):
        if self.wallet_transaction_id:
            self.wallet_transaction_id.unlink()
        self.state = 'cancelled'

    def action_reset(self):
        self.state = 'pending_approval'


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