from odoo import models, fields, api

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
    reference = fields.Char(string='Reference')
    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)

    