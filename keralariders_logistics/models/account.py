import logging

from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

# Transfers that settle COD to the seller (reduce portal pending when posted).
_COD_SETTLEMENT_TYPES = ('cod_clearance', 'cod_withdrawal', 'other')


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

    _ACTIVITY_TYPE_TODO = 'mail.mail_activity_data_todo'

    name = fields.Char(string="Reference", copy=False, default=lambda self: _('New'), readonly="1")
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('posted', 'Posted'),
            ('cancelled', 'Cancelled'),
        ],
        string='Status',
        default='posted',
        required=True,
        tracking=True,
        copy=False,
        index=True,
        help='Draft transfers (e.g. COD withdrawals) do not post ledger transactions until approved.',
    )

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
            if rec.transfer_type == 'cod_withdrawal' and rec.state == 'draft':
                rec._schedule_admin_approval_activities()
                rec._notify_admins_cod_withdrawal_request()
        return recs

    transfer_type = fields.Selection(
        selection=[
            ('cod_payment', 'COD Payment'),
            ('hub_deposit', 'Hub Deposit (DE → Hub)'),
            ('hub_banking', 'Hub Banking (Hub → Company)'),
            ('cod_clearance', 'COD Clearance (Company → Seller)'),
            ('cod_withdrawal', 'COD Withdrawal (Company → Seller)'),
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
            'cod_withdrawal': 'COD Withdrawal',
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

    @api.depends('from_account_id', 'to_account_id', 'amount', 'state')
    def _compute_transaction_ids(self):
        """Post debit/credit lines only for posted transfers (draft = no ledger impact)."""
        for rec in self:
            commands = [(2, tid) for tid in rec.transaction_ids.ids]
            if (
                rec.state == 'posted'
                and rec.from_account_id
                and rec.to_account_id
                and rec.amount
            ):
                commands.extend([
                    (0, 0, {
                        'account_id': rec.from_account_id.id,
                        'amount': -rec.amount,
                    }),
                    (0, 0, {
                        'account_id': rec.to_account_id.id,
                        'amount': rec.amount,
                    }),
                ])
            rec.transaction_ids = commands

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
            'state': 'posted',
            'cod_clearance_payment_transfer_ids': [(6, 0, payment_transfers.ids)],
        })

    @api.model
    def _seller_cod_balance_parts(self, seller):
        """Return (gross_payments, posted_settlements, draft_withdrawals) for a seller."""
        Transfer = self.sudo()
        payments = Transfer.search([
            ('related_seller_id', '=', seller.id),
            ('transfer_type', '=', 'cod_payment'),
            ('state', '=', 'posted'),
        ])
        settlements = Transfer.search([
            ('related_seller_id', '=', seller.id),
            ('transfer_type', 'in', list(_COD_SETTLEMENT_TYPES)),
            ('state', '=', 'posted'),
        ])
        draft_withdrawals = Transfer.search([
            ('related_seller_id', '=', seller.id),
            ('transfer_type', '=', 'cod_withdrawal'),
            ('state', '=', 'draft'),
        ])
        return (
            sum(payments.mapped('amount')),
            sum(settlements.mapped('amount')),
            sum(draft_withdrawals.mapped('amount')),
        )

    @api.model
    def get_seller_cod_pending_balance(self, seller):
        """Pending COD still owed to the seller's bank (payments − posted settlements)."""
        payments, settlements, _draft = self._seller_cod_balance_parts(seller)
        return max(0.0, payments - settlements)

    @api.model
    def get_seller_cod_withdrawable_balance(self, seller):
        """Amount available for a new withdrawal request (pending − draft withdrawals)."""
        payments, settlements, draft = self._seller_cod_balance_parts(seller)
        return max(0.0, payments - settlements - draft)

    @api.model
    def action_create_cod_withdrawal(self, seller, amount, note=None):
        """Seller portal: create a draft COD withdrawal (no ledger lines until approved)."""
        if not seller:
            raise UserError(_("Seller is required for COD withdrawal."))
        if not (seller.bank_account_name and seller.bank_account_number and seller.bank_ifsc):
            raise UserError(_(
                "Please update your bank details (account name, number, and IFSC) "
                "before requesting a COD withdrawal."
            ))
        company_account = self.env['logistics.account'].get_company_cod_account()
        if not company_account:
            raise UserError(_("No Company COD account configured."))
        seller_account = seller.seller_account_id
        if not seller_account:
            seller_account = self.env['logistics.account'].sudo().create({
                'name': f'{seller.name} Seller',
                'account_type': 'seller',
                'seller_id': seller.id,
                'reference': 'Auto-created seller COD settlement account',
            })
            seller.sudo().seller_account_id = seller_account.id
        available = self.get_seller_cod_withdrawable_balance(seller)
        if amount is None:
            amount = available
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            raise UserError(_("Invalid withdrawal amount."))
        if amount <= 0:
            raise UserError(_("Withdrawal amount must be positive."))
        if amount > available + 1e-6:
            raise UserError(_(
                "Withdrawal amount (%(amount)s) exceeds available COD balance (%(available)s).",
                amount=amount,
                available=available,
            ))
        bank_ref = _(
            "%(name)s / %(number)s / %(ifsc)s / %(bank)s",
            name=seller.bank_account_name or '',
            number=seller.bank_account_number or '',
            ifsc=seller.bank_ifsc or '',
            bank=seller.bank_name or '',
        )
        return self.sudo().create({
            'transfer_type': 'cod_withdrawal',
            'state': 'draft',
            'from_account_id': company_account.id,
            'to_account_id': seller_account.id,
            'amount': amount,
            'transfer_date': fields.Date.context_today(self),
            'reference': _('COD Withdrawal — %s') % seller.name,
            'description': note or _(
                "Seller COD withdrawal request to bank: %s"
            ) % bank_ref,
            'related_seller_id': seller.id,
        })

    def _get_logistics_admin_users(self):
        """Internal users in logistics admin group (excludes portal/public/system)."""
        admin_group = self.env.ref('keralariders_logistics.group_logistics_admin', raise_if_not_found=False)
        if not admin_group:
            return self.env['res.users']
        root_user = self.env.ref('base.user_root', raise_if_not_found=False)
        return admin_group.sudo().user_ids.filtered(
            lambda u: u.active and not u.share and (not root_user or u != root_user)
        )

    def _schedule_admin_approval_activities(self):
        """Create one To-Do activity per logistics admin for draft COD withdrawals."""
        try:
            admin_users = self._get_logistics_admin_users()
        except AccessError:
            _logger.warning(
                "Could not resolve logistics admin users for COD withdrawal activities "
                "(insufficient rights for %s); skipping activity schedule.",
                self.env.user.login,
                exc_info=True,
            )
            return self.env['mail.activity']
        if not admin_users:
            return self.env['mail.activity']
        activities = self.env['mail.activity']
        for transfer in self:
            amount = transfer.currency_id.format(transfer.amount) if transfer.currency_id else transfer.amount
            note = _(
                "Seller: %(seller)s<br/>"
                "Amount: %(amount)s<br/>"
                "Reference: %(reference)s<br/>"
                "Bank: %(bank)s",
                seller=transfer.related_seller_id.display_name or '',
                amount=amount,
                reference=transfer.name or '',
                bank=' / '.join(filter(None, [
                    transfer.related_seller_id.bank_account_name,
                    transfer.related_seller_id.bank_account_number,
                    transfer.related_seller_id.bank_ifsc,
                    transfer.related_seller_id.bank_name,
                ])) or _('Not set'),
            )
            for user in admin_users:
                activities |= transfer.sudo().activity_schedule(
                    self._ACTIVITY_TYPE_TODO,
                    summary=_('COD withdrawal pending approval'),
                    note=note,
                    user_id=user.id,
                )
        return activities

    def _complete_admin_approval_activities(self, feedback):
        self.sudo().activity_feedback(
            [self._ACTIVITY_TYPE_TODO],
            feedback=feedback,
        )

    def _notify_admins_cod_withdrawal_request(self):
        """Email / inbox notify logistics admins about a new COD withdrawal request."""
        try:
            admin_users = self._get_logistics_admin_users()
        except AccessError:
            _logger.warning(
                "Could not resolve logistics admin users for COD withdrawal email "
                "(insufficient rights for %s); skipping mail.",
                self.env.user.login,
                exc_info=True,
            )
            return
        partners = admin_users.mapped('partner_id').filtered(lambda p: p.email)
        if not partners:
            return
        for transfer in self:
            amount = transfer.currency_id.format(transfer.amount) if transfer.currency_id else transfer.amount
            body = _(
                "<p>A seller requested a COD withdrawal.</p>"
                "<ul>"
                "<li><strong>Seller:</strong> %(seller)s</li>"
                "<li><strong>Amount:</strong> %(amount)s</li>"
                "<li><strong>Reference:</strong> %(reference)s</li>"
                "</ul>"
                "<p>Please review and approve or cancel the draft transfer.</p>",
                seller=transfer.related_seller_id.display_name or '',
                amount=amount,
                reference=transfer.name or '',
            )
            transfer.sudo().message_notify(
                partner_ids=partners.ids,
                subject=_('COD withdrawal pending approval — %s') % (transfer.name or ''),
                body=body,
                email_layout_xmlid='mail.mail_notification_light',
            )

    def action_approve(self):
        """Approve draft transfer: post ledger transactions and complete activities."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only draft transfers can be approved."))
            if rec.amount <= 0:
                raise UserError(_("Transfer amount must be positive."))
            rec.write({'state': 'posted'})
            if rec.transfer_type == 'cod_withdrawal':
                rec._complete_admin_approval_activities(_('Approved'))
        return True

    def action_cancel_draft(self):
        """Cancel a draft transfer without posting ledger lines."""
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only draft transfers can be cancelled."))
            rec.write({'state': 'cancelled'})
            if rec.transfer_type == 'cod_withdrawal':
                rec._complete_admin_approval_activities(_('Cancelled'))
        return True


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
