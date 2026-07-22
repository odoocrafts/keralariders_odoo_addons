from odoo import models, fields, api, _

class BankCashAccount(models.Model):
    _name = "logistics.account"
    _description = 'Bank/Cash Account'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string="Account Name", required=True)
    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)    
    account_type = fields.Selection(selection=[('bank', 'Bank'), ('cash', 'Cash'), ('cod_customer', 'COD Customer Account'), ('seller', 'Seller Account')], required=True, string="Account Type", default="bank")
    reference = fields.Text(string="Account Reference")
    balance = fields.Monetary(string='Balance', compute="_compute_balance", currency_field='currency_id')

    def _compute_balance(self):
        for account in self:
            account.balance = sum(account.transaction_ids.mapped('amount'))

    total_credit = fields.Float(string='Total Credit', compute='_compute_total_credit')
    total_debit = fields.Float(string='Total Debit', compute='_compute_total_debit')
    def _compute_total_credit(self):
        for account in self:
            total_credit = sum(line.amount for line in account.transaction_ids if line.transaction_type == 'credit')
            account.total_credit = total_credit
    def _compute_total_debit(self):
        for account in self:
            total_debit = sum(line.amount for line in account.transaction_ids if line.transaction_type == 'debit')
            account.total_debit = total_debit

    def action_view_transfers(self):
        self.ensure_one()
        return {
            'name': 'Account Transfer',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.account.transfer',
            'view_mode': 'list,form',
            'domain': ['|', ('from_account_id', '=', self.id), ('to_account_id', '=', self.id)],
            'context': {'default_from_account_id': self.id, 'default_to_account_id': self.id},
        }

    def action_view_transactions(self):
        self.ensure_one()
        return {
            'name': 'Account Transactions',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.account.transaction',
            'view_mode': 'list,form',
            'domain': [('account_id', '=', self.id)],
            'context': {'default_account_id': self.id,},
        }

    transaction_ids = fields.One2many('logistics.account.transaction', 'account_id', string="Transactions")

    transfer_count = fields.Integer(compute="_compute_transfer_count")
    def _compute_transfer_count(self):
        for rec in self:
            rec.transfer_count = self.env['logistics.account.transfer'].search_count(['|', ('from_account_id', '=', self.id), ('to_account_id', '=', self.id)])

class BankCashAccountTransfer(models.Model):
    _name = "logistics.account.transfer"
    _description = 'Bank/Cash Account Transfer'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string="Reference", copy=False,  default=lambda self: _('New'), readonly="1" )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('logistics.account.transfer') or _('New')
        return super(BankCashAccountTransfer, self).create(vals_list)

    transfer_type = fields.Selection(selection=[('cod_payment', 'COD Payment'), ('cod_clearance', 'COD Clearance'), ('other', 'Other')], default='other', string="Transfer Type")
    from_account_id = fields.Many2one('logistics.account', string="From Account", required=True)
    to_account_id = fields.Many2one('logistics.account', string="To Account", required=True)
    amount = fields.Monetary(string='Amount', required=True, currency_field='currency_id',)

    # Compute amount when adding payment transfers to clear (Applicable when transfer_type = 'cod_clearance')
    @api.onchange('cod_clearance_payment_transfer_ids')
    def _onchange_cod_clearance_payment_transfer_ids(self):
        if self.transfer_type == 'cod_clearance':
            self.amount = sum(self.cod_clearance_payment_transfer_ids.mapped('amount'))

    @api.onchange('amount')
    def _onchange_amount(self):
        if self.amount < 0:
            # Make sure amount is always positive
            self.amount = -self.amount
    transfer_date = fields.Date(string='Transfer Date', default=fields.Date.context_today, required=True)
    description = fields.Text(string='Description')
    reference = fields.Text(string='Transfer Reference')
    @api.onchange('transfer_type')
    def _onchange_transfer_type(self):
        if self.transfer_type == 'cod_clearance':
            self.reference = 'COD Clearance'
            
    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)
    shipment_id = fields.Many2one('logistics.shipment', string="Related Shipment", ondelete="cascade")
    transaction_ids = fields.One2many('logistics.account.transaction', 'transfer_id', string="Transactions", compute="_compute_transaction_ids", store=True)

    @api.depends('from_account_id', 'to_account_id', 'amount')
    def _compute_transaction_ids(self):
        for rec in self:
            if rec.from_account_id and rec.to_account_id:
                rec.transaction_ids = [(2, id ) for id in rec.transaction_ids.ids]
                debit_values = {
                    'account_id': rec.from_account_id.id,
                    'amount': -rec.amount,
                }
                credit_values = {
                    'account_id': rec.to_account_id.id,
                    'amount': rec.amount,
                }
                rec.transaction_ids = [(0, 0, debit_values), (0, 0, credit_values)]

    related_seller_id = fields.Many2one('logistics.seller', string="Related Seller")

    cod_clearance_transfer_id = fields.Many2one('logistics.account.transfer', string="Clearance Transfer")
    cod_clearance_payment_transfer_ids = fields.Many2many(
        'logistics.account.transfer',
        'logistics_account_transfer_clearance_rel',  # relation table
        'clearance_id',                              # current model FK
        'payment_transfer_id',                       # related model FK
        string="Cleared COD Payments",
    )

    def write(self, vals):
        for rec in self:
            if rec.transfer_type == 'cod_clearance' and 'cod_clearance_payment_transfer_ids' in vals:
                old_cod_clearance_payment_transfer_ids = rec.cod_clearance_payment_transfer_ids
                super(BankCashAccountTransfer, rec).write(vals)
                new_cod_clearance_payment_transfer_ids = rec.cod_clearance_payment_transfer_ids
                if old_cod_clearance_payment_transfer_ids.ids != new_cod_clearance_payment_transfer_ids.ids:
                    for transfer_id in old_cod_clearance_payment_transfer_ids:
                        transfer_id.cod_clearance_transfer_id = False
                    for transfer_id in new_cod_clearance_payment_transfer_ids:
                        transfer_id.cod_clearance_transfer_id = rec.id
            else:
                super(BankCashAccountTransfer, rec).write(vals)
        return True


class BankCashAccountTransaction(models.Model):
    _name = "logistics.account.transaction"
    _description = 'Bank/Cash Account Transaction'

    account_id = fields.Many2one('logistics.account', string="Account", required=True)
    transaction_type = fields.Selection([('credit', 'Credit'), ('debit', 'Debit')], string='Transaction Type', default='credit', compute="_compute_transaction_type", store=True)
    @api.depends('amount')
    def _compute_transaction_type(self):
        for transaction in self:
            transaction.transaction_type = 'credit' if transaction.amount >= 0 else 'debit'
    amount = fields.Monetary(string='Amount', required=True, currency_field='currency_id')
    transaction_date = fields.Date(string='Transaction Date', related="transfer_id.transfer_date", store=True)
    description = fields.Text(string='Description', related="transfer_id.description", store=True)
    reference = fields.Text(string='Reference', related="transfer_id.reference", store=True)
    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)
    transfer_id = fields.Many2one('logistics.account.transfer', string="Related Transfer", ondelete="cascade")