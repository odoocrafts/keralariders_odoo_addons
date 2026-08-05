from odoo import models, fields, api, _
from odoo.exceptions import UserError


class BankCashAccount(models.Model):
    _name = "logistics.account"
    _description = 'Bank/Cash Account'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = "name,create_date"

    name = fields.Char(string="Account Name", required=True)
    currency_id = fields.Many2one(
        'res.currency', string='Currency',
        default=lambda self: self.env.company.currency_id.id,
    )
    account_type = fields.Selection(
        selection=[
            ('bank', 'Bank'),
            ('cash', 'Cash'),
            ('cod_customer', 'COD Customer Account'),
            ('seller', 'Seller Account'),
            ('hub', 'Hub Cash Account'),
            ('company', 'Company Account'),
        ],
        required=True,
        string="Account Type",
        default="bank",
    )
    reference = fields.Text(string="Account Reference")
    hub_id = fields.Many2one('logistics.hub', string="Related Hub", index=True, ondelete='set null')
    seller_id = fields.Many2one('logistics.seller', string="Related Seller", index=True, ondelete='set null')
    balance = fields.Monetary(string='Balance', compute="_compute_balance", currency_field='currency_id')

    def _compute_balance(self):
        for account in self:
            account.balance = sum(account.transaction_ids.mapped('amount'))

    total_credit = fields.Float(string='Total Credit', compute='_compute_total_credit')
    total_debit = fields.Float(string='Total Debit', compute='_compute_total_debit')

    def _compute_total_credit(self):
        for account in self:
            total_credit = sum(
                line.amount for line in account.transaction_ids if line.transaction_type == 'credit'
            )
            account.total_credit = total_credit

    def _compute_total_debit(self):
        for account in self:
            total_debit = sum(
                line.amount for line in account.transaction_ids if line.transaction_type == 'debit'
            )
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
            'context': {'default_account_id': self.id},
        }

    transaction_ids = fields.One2many('logistics.account.transaction', 'account_id', string="Transactions")

    transfer_count = fields.Integer(compute="_compute_transfer_count")

    def _compute_transfer_count(self):
        for rec in self:
            rec.transfer_count = self.env['logistics.account.transfer'].search_count([
                '|', ('from_account_id', '=', rec.id), ('to_account_id', '=', rec.id),
            ])

    @api.model
    def get_company_cod_account(self):
        """Resolve the company settlement account (settings → typed company account → bank)."""
        ICP = self.env['ir.config_parameter'].sudo()
        account_id = ICP.get_param('keralariders_logistics.company_cod_account_id')
        if account_id:
            account = self.browse(int(account_id)).exists()
            if account:
                return account
        account = self.search([('account_type', '=', 'company')], limit=1)
        if account:
            return account
        account = self.search([('account_type', '=', 'bank'), ('name', 'ilike', 'company')], limit=1)
        if account:
            return account
        return self.browse()


class BankCashAccountTransfer(models.Model):
    _name = "logistics.account.transfer"
    _description = 'Bank/Cash Account Transfer'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string="Reference", copy=False, default=lambda self: _('New'), readonly="1")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('logistics.account.transfer') or _('New')
        recs = super().create(vals_list)
        for rec in recs:
            if rec.transfer_type == 'cod_clearance' and rec.cod_clearance_payment_transfer_ids:
                for payment in rec.cod_clearance_payment_transfer_ids:
                    payment.cod_clearance_transfer_id = rec.id
            if rec.transfer_type == 'hub_deposit' and rec.hub_deposit_payment_transfer_ids:
                for payment in rec.hub_deposit_payment_transfer_ids:
                    payment.hub_deposit_transfer_id = rec.id
            if rec.transfer_type == 'hub_banking' and rec.hub_banking_deposit_transfer_ids:
                for deposit in rec.hub_banking_deposit_transfer_ids:
                    deposit.hub_banking_transfer_id = rec.id
                    # Propagate banking link onto underlying COD payments for clearance
                    for payment in deposit.hub_deposit_payment_transfer_ids:
                        payment.hub_banking_transfer_id = rec.id
        return recs

    transfer_type = fields.Selection(
        selection=[
            ('cod_payment', 'COD Payment'),
            ('hub_deposit', 'Hub Deposit (DE → Hub)'),
            ('hub_banking', 'Hub Banking (Hub → Company)'),
            ('cod_clearance', 'COD Clearance (Company → Seller)'),
            ('other', 'Other'),
        ],
        default='other',
        string="Transfer Type",
    )
    from_account_id = fields.Many2one('logistics.account', string="From Account", required=True)
    to_account_id = fields.Many2one('logistics.account', string="To Account", required=True)
    amount = fields.Monetary(string='Amount', required=True, currency_field='currency_id')
    hub_id = fields.Many2one('logistics.hub', string="Related Hub", index=True)

    @api.onchange('cod_clearance_payment_transfer_ids')
    def _onchange_cod_clearance_payment_transfer_ids(self):
        if self.transfer_type == 'cod_clearance':
            self.amount = sum(self.cod_clearance_payment_transfer_ids.mapped('amount'))

    @api.onchange('hub_deposit_payment_transfer_ids')
    def _onchange_hub_deposit_payment_transfer_ids(self):
        if self.transfer_type == 'hub_deposit':
            self.amount = sum(self.hub_deposit_payment_transfer_ids.mapped('amount'))

    @api.onchange('hub_banking_deposit_transfer_ids')
    def _onchange_hub_banking_deposit_transfer_ids(self):
        if self.transfer_type == 'hub_banking':
            self.amount = sum(self.hub_banking_deposit_transfer_ids.mapped('amount'))

    @api.onchange('amount')
    def _onchange_amount(self):
        if self.amount < 0:
            self.amount = -self.amount

    transfer_date = fields.Date(string='Transfer Date', default=fields.Date.context_today, required=True)
    description = fields.Text(string='Description')
    reference = fields.Text(string='Transfer Reference')

    @api.onchange('transfer_type')
    def _onchange_transfer_type(self):
        defaults = {
            'cod_clearance': 'COD Clearance',
            'hub_deposit': 'Hub Deposit (DE → Hub)',
            'hub_banking': 'Hub Banking (Hub → Company)',
            'cod_payment': 'COD Payment',
        }
        if self.transfer_type in defaults:
            self.reference = defaults[self.transfer_type]

    currency_id = fields.Many2one(
        'res.currency', string='Currency',
        default=lambda self: self.env.company.currency_id.id,
    )
    shipment_id = fields.Many2one('logistics.shipment', string="Related Shipment", ondelete="cascade")
    transaction_ids = fields.One2many(
        'logistics.account.transaction', 'transfer_id', string="Transactions",
        compute="_compute_transaction_ids", store=True,
    )

    @api.depends('from_account_id', 'to_account_id', 'amount')
    def _compute_transaction_ids(self):
        for rec in self:
            if rec.from_account_id and rec.to_account_id:
                rec.transaction_ids = [(2, tid) for tid in rec.transaction_ids.ids]
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

    # --- Settlement chain links ---
    # COD Payment → Hub Deposit → Hub Banking → COD Clearance
    hub_deposit_transfer_id = fields.Many2one(
        'logistics.account.transfer', string="Hub Deposit Transfer", copy=False, index=True,
    )
    hub_banking_transfer_id = fields.Many2one(
        'logistics.account.transfer', string="Hub Banking Transfer", copy=False, index=True,
    )
    cod_clearance_transfer_id = fields.Many2one(
        'logistics.account.transfer', string="Clearance Transfer", copy=False, index=True,
    )

    hub_deposit_payment_transfer_ids = fields.Many2many(
        'logistics.account.transfer',
        'logistics_account_transfer_hub_deposit_rel',
        'deposit_id',
        'payment_transfer_id',
        string="Deposited COD Payments",
    )
    hub_banking_deposit_transfer_ids = fields.Many2many(
        'logistics.account.transfer',
        'logistics_account_transfer_hub_banking_rel',
        'banking_id',
        'deposit_transfer_id',
        string="Banked Hub Deposits",
    )
    cod_clearance_payment_transfer_ids = fields.Many2many(
        'logistics.account.transfer',
        'logistics_account_transfer_clearance_rel',
        'clearance_id',
        'payment_transfer_id',
        string="Cleared COD Payments",
    )

    @api.onchange('from_account_id')
    def _onchange_from_account_settlement_lines(self):
        if not self.from_account_id:
            return
        Transfer = self.env['logistics.account.transfer']
        if self.transfer_type == 'cod_clearance':
            # Prefer payments that have completed hub banking into company account
            domain = [
                ('cod_clearance_transfer_id', '=', False),
                ('transfer_type', '=', 'cod_payment'),
            ]
            banked = Transfer.search(domain + [
                ('hub_banking_transfer_id', '!=', False),
                ('hub_banking_transfer_id.to_account_id', '=', self.from_account_id.id),
            ])
            if banked:
                self.cod_clearance_payment_transfer_ids = [(6, 0, banked.ids)]
            else:
                # Fallback: uncleared payments still sitting on from_account (legacy DE→seller path)
                uncleared = Transfer.search(domain + [
                    ('to_account_id', '=', self.from_account_id.id),
                ])
                self.cod_clearance_payment_transfer_ids = [(6, 0, uncleared.ids)]
        elif self.transfer_type == 'hub_deposit':
            undeposited = Transfer.search([
                ('hub_deposit_transfer_id', '=', False),
                ('transfer_type', '=', 'cod_payment'),
                ('to_account_id', '=', self.from_account_id.id),
            ])
            self.hub_deposit_payment_transfer_ids = [(6, 0, undeposited.ids)]
        elif self.transfer_type == 'hub_banking':
            unbanked = Transfer.search([
                ('hub_banking_transfer_id', '=', False),
                ('transfer_type', '=', 'hub_deposit'),
                ('to_account_id', '=', self.from_account_id.id),
            ])
            self.hub_banking_deposit_transfer_ids = [(6, 0, unbanked.ids)]

    def write(self, vals):
        for rec in self:
            if rec.transfer_type == 'cod_clearance' and 'cod_clearance_payment_transfer_ids' in vals:
                old_ids = rec.cod_clearance_payment_transfer_ids
                super(BankCashAccountTransfer, rec).write(vals)
                new_ids = rec.cod_clearance_payment_transfer_ids
                if old_ids.ids != new_ids.ids:
                    for transfer in old_ids:
                        transfer.cod_clearance_transfer_id = False
                    for transfer in new_ids:
                        transfer.cod_clearance_transfer_id = rec.id
            elif rec.transfer_type == 'hub_deposit' and 'hub_deposit_payment_transfer_ids' in vals:
                old_ids = rec.hub_deposit_payment_transfer_ids
                super(BankCashAccountTransfer, rec).write(vals)
                new_ids = rec.hub_deposit_payment_transfer_ids
                if old_ids.ids != new_ids.ids:
                    for transfer in old_ids:
                        transfer.hub_deposit_transfer_id = False
                    for transfer in new_ids:
                        transfer.hub_deposit_transfer_id = rec.id
            elif rec.transfer_type == 'hub_banking' and 'hub_banking_deposit_transfer_ids' in vals:
                old_ids = rec.hub_banking_deposit_transfer_ids
                super(BankCashAccountTransfer, rec).write(vals)
                new_ids = rec.hub_banking_deposit_transfer_ids
                if old_ids.ids != new_ids.ids:
                    for deposit in old_ids:
                        deposit.hub_banking_transfer_id = False
                        for payment in deposit.hub_deposit_payment_transfer_ids:
                            if payment.hub_banking_transfer_id == rec:
                                payment.hub_banking_transfer_id = False
                    for deposit in new_ids:
                        deposit.hub_banking_transfer_id = rec.id
                        for payment in deposit.hub_deposit_payment_transfer_ids:
                            payment.hub_banking_transfer_id = rec.id
            else:
                super(BankCashAccountTransfer, rec).write(vals)
        return True

    @api.model
    def action_create_hub_deposit(self, de, hub, payment_transfers=None, amount=None, note=None):
        """DE deposits COD cash holdings at a hub (DE cash → Hub cash)."""
        if not de or not de.default_cash_account_id:
            raise UserError(_("Delivery executive must have a cash account."))
        if not hub:
            raise UserError(_("Hub is required for COD cash deposit."))
        hub_account = hub.get_or_create_cash_account()
        Transfer = self.env['logistics.account.transfer']
        if payment_transfers is None:
            payment_transfers = Transfer.search([
                ('transfer_type', '=', 'cod_payment'),
                ('to_account_id', '=', de.default_cash_account_id.id),
                ('hub_deposit_transfer_id', '=', False),
            ])
        else:
            payment_transfers = payment_transfers.filtered(
                lambda t: t.transfer_type == 'cod_payment'
                and t.to_account_id == de.default_cash_account_id
                and not t.hub_deposit_transfer_id
            )
        if not payment_transfers and not amount:
            raise UserError(_("No undeposited COD cash payments found for this executive."))
        deposit_amount = amount if amount is not None else sum(payment_transfers.mapped('amount'))
        if deposit_amount <= 0:
            raise UserError(_("Deposit amount must be positive."))
        sellers = payment_transfers.mapped('related_seller_id')
        return Transfer.create({
            'transfer_type': 'hub_deposit',
            'from_account_id': de.default_cash_account_id.id,
            'to_account_id': hub_account.id,
            'hub_id': hub.id,
            'amount': deposit_amount,
            'transfer_date': fields.Date.context_today(self),
            'reference': _('Hub Deposit by %s at %s') % (de.name, hub.name),
            'description': note or _('COD cash deposited at hub.'),
            'related_seller_id': sellers[:1].id if len(sellers) == 1 else False,
            'hub_deposit_payment_transfer_ids': [(6, 0, payment_transfers.ids)],
        })

    @api.model
    def action_create_hub_banking(self, hub, deposit_transfers=None, amount=None, note=None):
        """Hub banks cash to company account (Hub cash → Company)."""
        if not hub:
            raise UserError(_("Hub is required for hub banking."))
        hub_account = hub.get_or_create_cash_account()
        company_account = self.env['logistics.account'].get_company_cod_account()
        if not company_account:
            raise UserError(_(
                "No Company COD account configured. "
                "Set it under Settings → Logistics, or create an account of type Company."
            ))
        Transfer = self.env['logistics.account.transfer']
        if deposit_transfers is None:
            deposit_transfers = Transfer.search([
                ('transfer_type', '=', 'hub_deposit'),
                ('to_account_id', '=', hub_account.id),
                ('hub_banking_transfer_id', '=', False),
            ])
        else:
            deposit_transfers = deposit_transfers.filtered(
                lambda t: t.transfer_type == 'hub_deposit'
                and t.to_account_id == hub_account
                and not t.hub_banking_transfer_id
            )
        if not deposit_transfers and not amount:
            raise UserError(_("No unbanked hub deposits found for this hub."))
        bank_amount = amount if amount is not None else sum(deposit_transfers.mapped('amount'))
        if bank_amount <= 0:
            raise UserError(_("Banking amount must be positive."))
        return Transfer.create({
            'transfer_type': 'hub_banking',
            'from_account_id': hub_account.id,
            'to_account_id': company_account.id,
            'hub_id': hub.id,
            'amount': bank_amount,
            'transfer_date': fields.Date.context_today(self),
            'reference': _('Hub Banking from %s') % hub.name,
            'description': note or _('Hub cash deposited to company bank.'),
            'hub_banking_deposit_transfer_ids': [(6, 0, deposit_transfers.ids)],
        })

    @api.model
    def action_create_cod_clearance(self, seller, payment_transfers=None, amount=None, note=None):
        """Clear COD from company account to seller settlement account."""
        if not seller:
            raise UserError(_("Seller is required for COD clearance."))
        company_account = self.env['logistics.account'].get_company_cod_account()
        if not company_account:
            raise UserError(_("No Company COD account configured."))
        seller_account = seller.seller_account_id
        if not seller_account:
            seller_account = self.env['logistics.account'].create({
                'name': f'{seller.name} Seller',
                'account_type': 'seller',
                'seller_id': seller.id,
            })
            seller.seller_account_id = seller_account.id
        Transfer = self.env['logistics.account.transfer']
        if payment_transfers is None:
            payment_transfers = Transfer.search([
                ('transfer_type', '=', 'cod_payment'),
                ('related_seller_id', '=', seller.id),
                ('cod_clearance_transfer_id', '=', False),
                ('hub_banking_transfer_id', '!=', False),
            ])
        if not payment_transfers and not amount:
            raise UserError(_("No banked, uncleared COD payments found for this seller."))
        clear_amount = amount if amount is not None else sum(payment_transfers.mapped('amount'))
        if clear_amount <= 0:
            raise UserError(_("Clearance amount must be positive."))
        return Transfer.create({
            'transfer_type': 'cod_clearance',
            'from_account_id': company_account.id,
            'to_account_id': seller_account.id,
            'amount': clear_amount,
            'transfer_date': fields.Date.context_today(self),
            'reference': _('COD Clearance to %s') % seller.name,
            'description': note or _('Company → Seller COD clearance.'),
            'related_seller_id': seller.id,
            'cod_clearance_payment_transfer_ids': [(6, 0, payment_transfers.ids)],
        })


class BankCashAccountTransaction(models.Model):
    _name = "logistics.account.transaction"
    _description = 'Bank/Cash Account Transaction'

    account_id = fields.Many2one('logistics.account', string="Account", required=True)
    transaction_type = fields.Selection(
        [('credit', 'Credit'), ('debit', 'Debit')],
        string='Transaction Type',
        default='credit',
        compute="_compute_transaction_type",
        store=True,
    )

    @api.depends('amount')
    def _compute_transaction_type(self):
        for transaction in self:
            transaction.transaction_type = 'credit' if transaction.amount >= 0 else 'debit'

    amount = fields.Monetary(string='Amount', required=True, currency_field='currency_id')
    transaction_date = fields.Date(string='Transaction Date', related="transfer_id.transfer_date", store=True)
    description = fields.Text(string='Description', related="transfer_id.description", store=True)
    reference = fields.Text(string='Reference', related="transfer_id.reference", store=True)
    currency_id = fields.Many2one(
        'res.currency', string='Currency',
        default=lambda self: self.env.company.currency_id.id,
    )
    transfer_id = fields.Many2one('logistics.account.transfer', string="Related Transfer", ondelete="cascade")
