import logging
from datetime import date, datetime, timezone as dt_timezone

from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError
from odoo.tools import html_escape
from odoo.tools.misc import format_date

_logger = logging.getLogger(__name__)

# Transfers that settle COD to the seller (reduce portal pending when posted).
_COD_SETTLEMENT_TYPES = ('cod_clearance', 'cod_withdrawal', 'other')

_MONTH_ABBR = (
    '', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
)


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

    @api.model
    def get_indiapost_cod_account(self):
        """The customer-side account India Post COD is collected into.

        Own-network COD moves as physical cash (customer → DE → hub → company),
        so its customer account is the cash or UPI one the DE settles through.
        India Post collects on our behalf and remits to the company, so its
        collection account is a single ledger counterparty rather than anyone's
        cash drawer.
        """
        account = self.sudo().env.ref(
            'keralariders_logistics.account_indiapost_cod_collection',
            raise_if_not_found=False,
        )
        if account:
            return account.sudo()
        account = self.sudo().search([
            ('account_type', '=', 'cod_customer'),
            ('name', 'ilike', 'india post'),
        ], limit=1)
        if account:
            return account
        return self.sudo().create({
            'name': 'India Post COD Collection',
            'account_type': 'cod_customer',
            'reference': 'COD collected by India Post on delivery and remitted '
                         'to KeralaXpress',
        })


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
    def _indiapost_cod_payment_for_shipment(self, shipment):
        """The COD payment already recorded for this shipment, if any.

        Looked up rather than trusted from a flag so a backfill (or a second
        upgrade) can never credit the same article twice, even on a shipment
        whose flag was lost.
        """
        if not shipment:
            return self.browse()
        return self.sudo().search([
            ('transfer_type', '=', 'cod_payment'),
            ('shipment_id', '=', shipment.id),
            ('state', '!=', 'cancelled'),
        ], limit=1)

    @api.model
    def action_create_indiapost_cod_payment(self, shipment):
        """Credit the seller's COD ledger for one delivered India Post article.

        India Post hands the money to the company, not to a delivery executive,
        so this books the single leg that actually happened — collection
        account → company account — and leaves the DE and hub cash custody
        steps out. The seller side is ``related_seller_id``, which is what
        ``_seller_cod_balance_parts`` sums for the portal balance, so the
        shipment shows up at /my/cod_settlements as "COD collected from
        customer" exactly like an own-network delivery.

        Returns the existing payment when there already is one.
        """
        if not shipment:
            return self.browse()
        shipment = shipment.sudo()
        existing = self._indiapost_cod_payment_for_shipment(shipment)
        if existing:
            return existing
        if not shipment.seller_id:
            raise UserError(_(
                "Shipment %s has no seller, so its COD cannot be credited."
            ) % shipment.name)
        amount = shipment.cod_amount
        if amount <= 0:
            raise UserError(_(
                "Shipment %s has no COD amount to credit."
            ) % shipment.name)
        from_account = self.env['logistics.account'].get_indiapost_cod_account()
        to_account = self.env['logistics.account'].get_company_cod_account()
        if not to_account:
            raise UserError(_(
                "No Company COD account configured. "
                "Set it under Settings → Logistics, or create an account of "
                "type Company."
            ))
        transfer = self.sudo().create({
            'transfer_type': 'cod_payment',
            'state': 'posted',
            'from_account_id': from_account.id,
            'to_account_id': to_account.id,
            'amount': amount,
            'transfer_date': fields.Date.to_date(
                shipment.delivered_on or shipment.actual_delivery_date
            ) or fields.Date.context_today(self),
            'reference': _('India Post COD — %s') % (
                shipment.indiapost_article_number or shipment.name),
            'description': _(
                'COD collected by India Post on delivery of %s and remitted '
                'to KeralaXpress.'
            ) % shipment.name,
            'related_seller_id': shipment.seller_id.id,
            'shipment_id': shipment.id,
        })
        shipment.cod_payment_transfer_ids = [(4, transfer.id)]
        return transfer

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

    # ------------------------------------------------------------------
    # COD withdrawal settlement cycle
    #
    # A request is tied to the cycle of its request date (seller tz). Money is
    # scheduled for a fixed settlement date — not "anytime / next 24 hours".
    # One open (draft or approved-unpaid) withdrawal per seller per cycle.
    # ------------------------------------------------------------------
    @api.model
    def _cod_withdrawal_tz_name(self, seller=None):
        """Timezone for the request-date calendar day (UTC only when none set)."""
        if seller:
            partner = seller.partner_id
            if partner:
                user = self.env['res.users'].sudo().search(
                    [('partner_id', '=', partner.id)], limit=1)
                if user and user.tz:
                    return user.tz
                if partner.tz:
                    return partner.tz
        company_partner = self.env.company.partner_id
        if company_partner and company_partner.tz:
            return company_partner.tz
        if self.env.user.tz:
            return self.env.user.tz
        return False

    @api.model
    def _cod_withdrawal_request_date(self, seller=None):
        """Today's date in the seller/company/user timezone (UTC if none)."""
        tz_name = self._cod_withdrawal_tz_name(seller)
        if tz_name:
            return fields.Date.context_today(self.with_context(tz=tz_name))
        # Explicit UTC calendar day — do not fall through to a later user.tz.
        return fields.Date.to_date(datetime.now(dt_timezone.utc).date())

    @api.model
    def _cod_settlement_date_for_request_date(self, request_date):
        """Map a request calendar date to its fixed COD settlement date.

        | Request dates              | Settlement date                          |
        | 6th–15th                   | 20th of the same month                   |
        | 16th–25th                  | 30th same month (Feb → 28, never 29)     |
        | 26th–end, and 1st–5th      | 10th (next month if 26–31; this if 1–5)  |
        """
        d = fields.Date.to_date(request_date)
        day, year, month = d.day, d.year, d.month
        if 6 <= day <= 15:
            return date(year, month, 20)
        if 16 <= day <= 25:
            if month == 2:
                return date(year, 2, 28)
            return date(year, month, 30)
        if day >= 26:
            if month == 12:
                return date(year + 1, 1, 10)
            return date(year, month + 1, 10)
        # 1st–5th → 10th of this month
        return date(year, month, 10)

    @api.model
    def _format_cod_day_month(self, value):
        """Short calendar label like ``7 Sep`` / ``20 Sep`` (no year)."""
        d = fields.Date.to_date(value)
        return '%s %s' % (d.day, _MONTH_ABBR[d.month])

    @api.model
    def _cod_settlement_cycle_window(self, request_date):
        """Return (window_start, window_end) covering the cycle of ``request_date``."""
        d = fields.Date.to_date(request_date)
        day, year, month = d.day, d.year, d.month
        if 6 <= day <= 15:
            return date(year, month, 6), date(year, month, 15)
        if 16 <= day <= 25:
            return date(year, month, 16), date(year, month, 25)
        # 26–end + 1–5 share the 10th settlement
        if day >= 26:
            if month == 12:
                return date(year, month, 26), date(year + 1, 1, 5)
            return date(year, month, 26), date(year, month + 1, 5)
        # 1–5: window started on the 26th of the previous month
        if month == 1:
            return date(year - 1, 12, 26), date(year, month, 5)
        return date(year, month - 1, 26), date(year, month, 5)

    @api.model
    def get_cod_settlement_cycle_info(self, seller=None, request_date=None):
        """Portal helpers: current cycle window + settlement date if requesting now."""
        if request_date is None:
            request_date = self._cod_withdrawal_request_date(seller)
        else:
            request_date = fields.Date.to_date(request_date)
        settlement = self._cod_settlement_date_for_request_date(request_date)
        window_start, window_end = self._cod_settlement_cycle_window(request_date)
        start_label = self._format_cod_day_month(window_start)
        end_label = self._format_cod_day_month(window_end)
        settle_label = self._format_cod_day_month(settlement)
        summary = _(
            "Requests from %(start)s–%(end)s settle on %(settle)s.",
            start=start_label,
            end=end_label,
            settle=settle_label,
        )
        return {
            'request_date': request_date,
            'settlement_date': settlement,
            'window_start': window_start,
            'window_end': window_end,
            'summary': summary,
            'settlement_label': settle_label,
        }

    @api.model
    def _find_open_cod_withdrawal_for_settlement(self, seller, settlement_date):
        """Draft or approved-unpaid withdrawal for this seller + settlement date."""
        return self.sudo().search([
            ('related_seller_id', '=', seller.id),
            ('transfer_type', '=', 'cod_withdrawal'),
            ('cod_settlement_date', '=', settlement_date),
            ('cod_payout_state', 'in', ('requested', 'approved')),
        ], limit=1)

    @api.model
    def seller_has_open_cod_withdrawal_this_cycle(self, seller, request_date=None):
        """Whether the seller already has an open request for this cycle's settlement."""
        if request_date is None:
            request_date = self._cod_withdrawal_request_date(seller)
        settlement = self._cod_settlement_date_for_request_date(request_date)
        return bool(self._find_open_cod_withdrawal_for_settlement(seller, settlement))

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
        request_date = self._cod_withdrawal_request_date(seller)
        settlement_date = self._cod_settlement_date_for_request_date(request_date)
        open_same_cycle = self._find_open_cod_withdrawal_for_settlement(
            seller, settlement_date)
        if open_same_cycle:
            settle_label = format_date(
                self.env, settlement_date, date_format='d MMMM y')
            raise UserError(_(
                "You already have a COD withdrawal scheduled for settlement on "
                "%(date)s (request %(ref)s). Only one open request is allowed "
                "per settlement cycle. Wait until it is paid or cancelled before "
                "requesting again for this cycle.",
                date=settle_label,
                ref=open_same_cycle.name or '',
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
            'transfer_date': request_date,
            'cod_settlement_date': settlement_date,
            'reference': _('COD Withdrawal — %s') % seller.name,
            'description': note or _(
                "Seller COD withdrawal request to bank: %s"
            ) % bank_ref,
            'related_seller_id': seller.id,
        })

    # ------------------------------------------------------------------
    # Withdrawal payout
    #
    # Approving a withdrawal posts the ledger — that is the company committing
    # to the payout — but the money only leaves the bank when finance actually
    # transfers it. Those are two different facts, so the second one is stamped
    # separately instead of being folded into ``state``: a ``paid`` state would
    # have dropped the transfer out of ``_compute_transaction_ids`` and taken
    # the ledger lines (and the seller's settled balance) with it.
    # ------------------------------------------------------------------
    cod_settlement_date = fields.Date(
        string='Settlement Date',
        readonly=True,
        copy=False,
        tracking=True,
        index=True,
        help='Fixed payout date for this COD withdrawal, set from the request '
             'date cycle when the seller submits. Not recomputed later.',
    )
    cod_paid_on = fields.Datetime(
        string='Paid On', readonly=True, copy=False, tracking=True,
        help='When finance confirmed the bank transfer actually left the '
             'company account.',
    )
    cod_paid_by = fields.Many2one(
        'res.users', string='Paid By', readonly=True, copy=False, tracking=True,
    )
    cod_paid_reference = fields.Char(
        string='Bank Payment Reference', copy=False, tracking=True,
        help='UTR / NEFT reference of the transfer to the seller.',
    )
    cod_payout_state = fields.Selection(
        selection=[
            ('requested', 'Requested'),
            ('approved', 'Approved'),
            ('paid', 'Paid'),
            ('cancelled', 'Cancelled'),
        ],
        string='Payout Status',
        compute='_compute_cod_payout_state',
        store=True,
        help='Seller-facing reading of a COD withdrawal: requested → approved '
             '→ paid.',
    )

    @api.depends('state', 'cod_paid_on', 'transfer_type')
    def _compute_cod_payout_state(self):
        for rec in self:
            if rec.transfer_type != 'cod_withdrawal':
                rec.cod_payout_state = False
            elif rec.state == 'cancelled':
                rec.cod_payout_state = 'cancelled'
            elif rec.state == 'draft':
                rec.cod_payout_state = 'requested'
            elif rec.cod_paid_on:
                rec.cod_payout_state = 'paid'
            else:
                rec.cod_payout_state = 'approved'

    def action_mark_cod_paid(self):
        """Finance confirms the bank transfer promised at approval has gone out."""
        for rec in self:
            if rec.transfer_type != 'cod_withdrawal':
                raise UserError(_(
                    "Only COD withdrawals can be marked paid."
                ))
            if rec.state != 'posted':
                raise UserError(_(
                    "Withdrawal %s must be approved before it can be marked paid."
                ) % (rec.name or ''))
            if rec.cod_paid_on:
                continue
            rec.write({
                'cod_paid_on': fields.Datetime.now(),
                'cod_paid_by': self.env.user.id,
            })
            rec.message_post(body=_(
                'Marked paid to the seller bank account by %s.'
            ) % self.env.user.name)
        return True

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
        """Create one To-Do per logistics admin. Assignment email is suppressed."""
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
                "Settlement date: %(settle)s<br/>"
                "Bank: %(bank)s",
                seller=transfer.related_seller_id.display_name or '',
                amount=amount,
                reference=transfer.name or '',
                settle=format_date(
                    self.env, transfer.cod_settlement_date, date_format='d MMMM y'
                ) if transfer.cod_settlement_date else _('Not set'),
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

    def _cod_bank_reference(self):
        """Readable account line for the seller this withdrawal pays."""
        self.ensure_one()
        seller = self.related_seller_id.sudo()
        return ' / '.join(filter(None, [
            seller.bank_account_name,
            seller.bank_account_number,
            seller.bank_ifsc,
            seller.bank_name,
        ])) or _('Not set')

    def _notify_admins_cod_withdrawal_request(self):
        """Queue a team email for a new COD withdrawal (company From, not seller)."""
        Mail = self.env['logistics.mail.notify'].sudo()
        email_to = Mail._kx_team_email_to()
        if not email_to:
            _logger.warning(
                'Skipping COD withdrawal team email: no ops/admin/company recipient.'
            )
            return
        base = Mail._kx_base_url()
        action = self.sudo().env.ref(
            'keralariders_logistics.action_logistics_account_transfer_other',
            raise_if_not_found=False,
        )
        list_url = '%s/odoo/m-logistics.account.transfer' % base
        if action:
            list_url = '%s/odoo/action-%s' % (base, action.id)
        for transfer in self:
            try:
                amount = (
                    transfer.currency_id.format(transfer.amount)
                    if transfer.currency_id else transfer.amount
                )
                seller_email = (
                    transfer.related_seller_id.email
                    or transfer.related_seller_id.partner_id.email
                    or ''
                ).strip()
                form_url = '%s/%s' % (list_url, transfer.id) if transfer.id \
                    else list_url
                settle_label = (
                    format_date(
                        self.env, transfer.cod_settlement_date, date_format='d MMMM y')
                    if transfer.cod_settlement_date else _('Not set')
                )
                inner = Markup(
                    '<p>A seller requested a COD withdrawal.</p>'
                    '<ul>'
                    '<li><strong>Seller:</strong> %s</li>'
                    '<li><strong>Amount:</strong> %s</li>'
                    '<li><strong>Reference:</strong> %s</li>'
                    '<li><strong>Settlement date:</strong> %s</li>'
                    '<li><strong>Bank account:</strong> %s</li>'
                    '</ul>'
                    '<p>Open the request: <a href="%s">%s</a><br/>'
                    'Or review the withdrawal list: <a href="%s">%s</a></p>'
                    '<p>Please review and approve or cancel the draft transfer.</p>'
                ) % (
                    html_escape(transfer.related_seller_id.display_name or ''),
                    html_escape(str(amount)),
                    html_escape(transfer.name or ''),
                    html_escape(settle_label),
                    html_escape(transfer._cod_bank_reference()),
                    html_escape(form_url),
                    html_escape(form_url),
                    html_escape(list_url),
                    html_escape(list_url),
                )
                Mail._kx_queue_mail(
                    email_to=email_to,
                    subject=_('COD withdrawal pending approval — %s') % (
                        transfer.name or ''),
                    body_html=Mail._kx_wrap_body(
                        _('COD withdrawal pending approval'), inner),
                    reply_to=seller_email or False,
                    res_model=self._name,
                    res_id=transfer.id,
                )
            except Exception:
                _logger.exception(
                    'Failed to queue COD withdrawal team email for %s',
                    transfer.name,
                )

    def _notify_seller_cod_withdrawal_approved(self):
        """Tell the seller the approved amount is scheduled for their bank.

        Sent once, from :meth:`action_approve`, which refuses anything that is
        not still draft — so a second approve cannot produce a second mail.
        """
        Mail = self.env['logistics.mail.notify'].sudo()
        for transfer in self:
            seller = transfer.related_seller_id.sudo()
            email = (
                seller.email or seller.partner_id.email or ''
            ).strip()
            if not email or '@' not in email:
                continue
            try:
                amount = (
                    transfer.currency_id.format(transfer.amount)
                    if transfer.currency_id else transfer.amount
                )
                settle_label = (
                    format_date(
                        self.env, transfer.cod_settlement_date, date_format='d MMMM y')
                    if transfer.cod_settlement_date else _('Not set')
                )
                inner = Markup(
                    '<p>Hello %s,</p>'
                    '<p>Your COD withdrawal request has been approved. The '
                    'amount will be credited on %s.</p>'
                    '<ul>'
                    '<li><strong>Request ref:</strong> %s</li>'
                    '<li><strong>Amount approved:</strong> %s</li>'
                    '<li><strong>Settlement date:</strong> %s</li>'
                    '<li><strong>Bank account:</strong> %s</li>'
                    '</ul>'
                    '<p>You can follow the payout under COD Settlements in '
                    'your seller portal.</p>'
                ) % (
                    html_escape(seller.display_name or ''),
                    html_escape(settle_label),
                    html_escape(transfer.name or ''),
                    html_escape(str(amount)),
                    html_escape(settle_label),
                    html_escape(transfer._cod_bank_reference()),
                )
                Mail._kx_queue_mail(
                    email_to=email,
                    subject=_('COD withdrawal approved — %s') % (
                        transfer.name or ''),
                    body_html=Mail._kx_wrap_body(
                        _('Your COD withdrawal has been approved'), inner),
                    reply_to=Mail._kx_support_email() or False,
                    res_model=self._name,
                    res_id=transfer.id,
                )
            except Exception:
                _logger.exception(
                    'Failed to queue COD withdrawal approval email for %s',
                    transfer.name,
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
                rec._notify_seller_cod_withdrawal_approved()
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
