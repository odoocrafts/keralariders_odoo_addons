from odoo import models, fields, api

class Wallet(models.Model):
    _name = 'logistics.wallet'
    _description = 'Wallet'

    name = fields.Char(string='Wallet Name', required=True, compute='_compute_wallet_name', store=True, readonly=False)
    
    @api.depends('seller_id')
    def _compute_wallet_name(self):
        for wallet in self:
            wallet.name = f"{wallet.seller_id.name} - Wallet" if wallet.seller_id else ''
    seller_id = fields.Many2one('logistics.seller', string='Seller', required=True)
    transaction_ids = fields.One2many('logistics.wallet.transaction', 'wallet_id', string='Transactions')
    balance = fields.Float(string='Balance', compute="_compute_balance", store=True, readonly=False)

    @api.depends('transaction_ids.amount')
    def _compute_balance(self):
        for wallet in self:
            wallet.balance = sum(transaction.amount for transaction in wallet.transaction_ids if transaction.transaction_type == 'credit') - sum(transaction.amount for transaction in wallet.transaction_ids if transaction.transaction_type == 'debit')

class WalletTransaction(models.Model):
    _name = 'logistics.wallet.transaction'
    _description = 'Wallet Transaction'

    wallet_id = fields.Many2one('logistics.wallet', string='Wallet', required=True)
    transaction_type = fields.Selection([('credit', 'Credit'), ('debit', 'Debit')], string='Transaction Type', default='credit', compute="_compute_transaction_type", store=True)
    @api.depends('amount')
    def _compute_transaction_type(self):
        for transaction in self:
            transaction.transaction_type = 'credit' if transaction.amount >= 0 else 'debit'
    amount = fields.Float(string='Amount', required=True)
    transaction_date = fields.Date(string='Transaction Date', default=fields.Date.context_today, required=True)
    description = fields.Text(string='Description')
    reference = fields.Char(string='Reference')

    