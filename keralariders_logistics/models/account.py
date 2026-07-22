from odoo import models, fields, api

class BankCashAccount(models.Model):
    _name = "logistics.account"
    _description = 'Bank/Cash Account'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string="Account Name", required=True)
    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)    
    account_type = fields.Selection(selection=[('bank', 'Bank'), ('cash', 'Cash'), ('cod_customer', 'COD Customer Account'), ('seller', 'Seller Account')])
    reference = fields.Text(string="Account Reference")
    balance = fields.Monetary(string='Balance', compute="_compute_balance", currency_field='currency_id')

    def _compute_balance(self):
        for account in self:
            account.balance = sum(account.transaction_line_ids.mapped('amount'))

    total_credit = fields.Float(string='Total Credit', compute='_compute_total_credit')
    total_debit = fields.Float(string='Total Debit', compute='_compute_total_debit')
    def _compute_total_credit(self):
        for account in self:
            total_credit = sum(line.amount for line in account.transaction_line_ids if line.transaction_type == 'credit')
            account.total_credit = total_credit
    def _compute_total_debit(self):
        for account in self:
            total_debit = sum(line.amount for line in account.transaction_line_ids if line.transaction_type == 'debit')
            account.total_debit = total_debit

    def action_view_transactions(self):
        self.ensure_one()
        return {
            'name': 'Account Transactions',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.account.transaction',
            'view_mode': 'list,form',
            'domain': ['|', ('from_account_id', '=', self.id), ('to_account_id', '=', self.id)],
            'context': {'default_from_account_id': self.id, 'default_to_account_id': self.id},
        }

    def action_view_transaction_lines(self):
        self.ensure_one()
        return {
            'name': 'Account Transaction Lines',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.account.transaction.line',
            'view_mode': 'list,form',
            'domain': [('account_id', '=', self.id)],
            'context': {'default_account_id': self.id,},
        }

    transaction_line_ids = fields.One2many('logistics.account.transaction.line', 'account_id', string="Transaction Lines")

    transaction_count = fields.Integer(compute="_compute_transaction_count")
    def _compute_transaction_count(self):
        for rec in self:
            rec.transaction_count = self.env['logistics.account.transaction'].search_count(['|', ('from_account_id', '=', self.id), ('to_account_id', '=', self.id)])

class BankCashAccountTransaction(models.Model):
    _name = "logistics.account.transaction"
    _description = 'Bank/Cash Account Transaction'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    from_account_id = fields.Many2one('logistics.account', string="From Account", required=True)
    to_account_id = fields.Many2one('logistics.account', string="To Account", required=True)
    amount = fields.Monetary(string='Amount', required=True, currency_field='currency_id')

    @api.onchange('amount')
    def _onchange_amount(self):
        if self.amount < 0:
            # Make sure amount is always positive
            self.amount = -self.amount
    transaction_date = fields.Date(string='Transaction Date', default=fields.Date.context_today, required=True)
    description = fields.Text(string='Description')
    reference = fields.Text(string='Reference')
    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)
    shipment_id = fields.Many2one('logistics.shipment', string="Related Shipment", ondelete="cascade")
    line_ids = fields.One2many('logistics.account.transaction.line', 'transaction_id', string="Transaction Lines", compute="_compute_line_ids", store=True)

    @api.depends('from_account_id', 'to_account_id', 'amount')
    def _compute_line_ids(self):
        for rec in self:
            if rec.from_account_id and rec.to_account_id:
                rec.line_ids = [(2, id ) for id in rec.line_ids.ids]
                debit_values = {
                    'account_id': rec.from_account_id.id,
                    'amount': -rec.amount,
                }
                credit_values = {
                    'account_id': rec.to_account_id.id,
                    'amount': rec.amount,
                }
                rec.line_ids = [(0, 0, debit_values), (0, 0, credit_values)]

    related_seller_id = fields.Many2one('logistics.seller', string="Related Seller")

class BankCashAccountTransactionLine(models.Model):
    _name = "logistics.account.transaction.line"
    _description = 'Bank/Cash Account Transaction Line'

    account_id = fields.Many2one('logistics.account', string="Account", required=True)
    transaction_type = fields.Selection([('credit', 'Credit'), ('debit', 'Debit')], string='Transaction Type', default='credit', compute="_compute_transaction_type", store=True)
    @api.depends('amount')
    def _compute_transaction_type(self):
        for transaction in self:
            transaction.transaction_type = 'credit' if transaction.amount >= 0 else 'debit'
    amount = fields.Monetary(string='Amount', required=True, currency_field='currency_id')
    transaction_date = fields.Date(string='Transaction Date', related="transaction_id.transaction_date", store=True)
    description = fields.Text(string='Description', related="transaction_id.description", store=True)
    reference = fields.Text(string='Reference', related="transaction_id.reference", store=True)
    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)
    transaction_id = fields.Many2one('logistics.account.transaction', string="Related Transaction", ondelete="cascade")