import logging

from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

# A recharge request is an instruction to put money into a wallet. These are
# the fields that decide how much lands there, whether an approval happened at
# all, and who is recorded as having made it. ``recharged_amount`` is what
# action_approve_request credits verbatim; ``state`` and
# ``wallet_transaction_id`` are the two sentinels that say the credit has (or
# has not) already been paid out; ``approved_by``/``approved_date`` are the
# provenance an administrator's approval leaves behind, and are guarded with
# the rest because a forged stamp is how a self-approved request would be made
# to look routine in the backend list.
RECHARGE_APPROVAL_FIELDS = (
    'recharged_amount',
    'state',
    'wallet_transaction_id',
    'approved_by',
    'approved_date',
)

# Only a Logistics Administrator decides that money enters a wallet. Deliberately
# not "any non-portal user" and not "not group_portal": hub managers and delivery
# executives are themselves portal accounts, so naming the admin group is both
# narrower and the same group the delivery charge guard already names.
RECHARGE_ADMIN_GROUP = 'keralariders_logistics.group_logistics_admin'


class Wallet(models.Model):
    _name = 'logistics.wallet'
    _description = 'Wallet'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = "has_pending_recharge_requests desc,name"

    name = fields.Char(string='Wallet Name', required=True, compute='_compute_wallet_name', store=True, readonly=False)
    
    @api.depends('seller_id')
    def _compute_wallet_name(self):
        for wallet in self:
            wallet.name = f"{wallet.seller_id.name} - Wallet" if wallet.seller_id else ''
    seller_id = fields.Many2one('logistics.seller', string='Seller', required=True, ondelete="cascade")
    transaction_ids = fields.One2many('logistics.wallet.transaction', 'wallet_id', string='Transactions')
    balance = fields.Monetary(string='Balance', compute="_compute_balance", currency_field='currency_id')

    def _compute_balance(self):
        for wallet in self:
            wallet.balance = sum(wallet.transaction_ids.mapped('amount'))

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
            rec.pending_recharge_request_count = len(rec.wallet_recharge_request_ids.filtered(lambda req: req.state == 'pending_approval'))
        
    def action_view_recharge_requests(self):
        self.ensure_one()
        return {
            'name': 'Wallet Recharge Requests',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.wallet.recharge.request',
            'view_mode': 'list,form',
            'domain': [('seller_id', '=', self.seller_id.id), ('wallet_id', '=', self.id)],
            'context': {'default_seller_id': self.seller_id.id, 'default_wallet_id': self.id, 'search_default_pending': 1},
        }

    has_pending_recharge_requests = fields.Boolean(string="Has Pending Recharge Requests", compute="_compute_has_pending_recharge_requests", store=True, readonly=False)

    @api.depends('wallet_recharge_request_ids.state')
    def _compute_has_pending_recharge_requests(self):
        for record in self:
            if 'pending_approval' in self.wallet_recharge_request_ids.mapped('state'):
                record.has_pending_recharge_requests = True
            else:
                record.has_pending_recharge_requests = False

    pending_recharge_request_count = fields.Integer(compute="_compute_wallet_recharge_request_count")
    
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
    order_id = fields.Many2one('logistics.order', string="Related Order", ondelete="cascade")
    recharge_request_id = fields.Many2one('logistics.wallet.recharge.request', string="Recharge Request", ondelete="cascade")
    
class WalletRechargeRequest(models.Model):
    _name = "logistics.wallet.recharge.request"
    _description = "Wallet Recharge Request"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    name = fields.Char(string="Reference", readonly=True, store=True, copy=False, default=lambda self: _('New'))

    _ACTIVITY_TYPE_TODO = 'mail.mail_activity_data_todo'

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Portal sellers hold create on this model — that is the recharge
            # journey — so a request must not be able to arrive already
            # approved, or already carrying an amount nobody agreed to.
            self._check_recharge_approval_write(vals)
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].sudo().next_by_code('logistics.wallet.recharge.request') or _('New')
        records = super(WalletRechargeRequest, self).create(vals_list)
        pending = records.filtered(lambda r: r.state == 'pending_approval')
        pending._schedule_admin_approval_activities()
        pending._notify_admins_recharge_request()
        return records

    def write(self, vals):
        self._check_recharge_approval_write(vals)
        self._check_requested_amount_write(vals)
        return super().write(vals)
    
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

    # -------------------------------------------------------------------------
    # Recharge approval integrity
    #
    # This is the money-in mirror of the delivery charge guard on
    # logistics.shipment, and it is guarded the same way for the same reason.
    # Portal sellers hold read/write/create on this model because raising a
    # recharge request *is* the portal top-up journey, and ``recharged_amount``
    # is a stored computed field with ``readonly=False`` whose compute depends
    # only on ``requested_amount``. So a seller could request ₹100 and then, in
    # a second write, set ``recharged_amount`` to ₹100,000: the compute never
    # re-ran because the amount it depends on had not changed, and an
    # administrator opening the request was shown the seller's figure as though
    # it were the system's. Approving credited it verbatim.
    #
    # The fix is provenance, not arithmetic. Once ``recharged_amount`` can only
    # be written by a Logistics Administrator, a value that differs from
    # ``requested_amount`` is *by definition* something staff typed, so the
    # legitimate ops case — a seller asks for ₹100, actually transfers ₹98, an
    # administrator corrects the figure before approving — keeps working with
    # no value-based heuristic to tune and no honest correction to explain away.
    #
    # The check reads ``env.user`` rather than the superuser flag, because every
    # portal controller runs ``sudo()`` and ``sudo()`` leaves ``env.user`` as the
    # real user. Trusted server code opts in with ``allow_recharge_approval_write``,
    # the same convention ``allow_shipment_state_write`` and
    # ``allow_delivery_charge_write`` already use.
    #
    # ``action_approve_request`` carries its own copy of the group check rather
    # than leaning on the guard above. It is a public method, reachable over RPC
    # by any portal seller on their own request, and until now the only thing
    # stopping it crediting them was that portal lacks ``create`` on
    # logistics.wallet.transaction — a single digit in ir.model.access.csv, one
    # unrelated feature away from turning this into self-service.
    # -------------------------------------------------------------------------
    def _can_approve_recharge(self):
        """Whether the current user may decide that money enters a wallet."""
        if self.env.context.get('allow_recharge_approval_write'):
            return True
        return self.env.user.has_group(RECHARGE_ADMIN_GROUP)

    def _check_recharge_admin(self, message):
        """Refuse an approval-side action to everyone but logistics staff."""
        if not self._can_approve_recharge():
            raise AccessError(message)

    def _check_recharge_approval_write(self, vals):
        attempted = [name for name in RECHARGE_APPROVAL_FIELDS if name in vals]
        if attempted and not self._can_approve_recharge():
            raise AccessError(_(
                "Only KeralaXpress can decide what a wallet is credited and "
                "when (%(fields)s). State the amount you are paying in "
                "'Amount Requested'; the recharge is credited once your "
                "payment has been verified.",
                fields=', '.join(attempted),
            ))

    def _check_requested_amount_write(self, vals):
        """Freeze the seller's declaration once the request has been decided.

        ``requested_amount`` stays seller-writable while the request is pending
        — it is the seller's own statement of what they are paying, and it
        drives the compute behind ``recharged_amount``, so freezing it earlier
        would break correcting a typo before anyone has looked. After approval
        it describes a payment that has already been reconciled and credited,
        and after cancellation it is a closed record; editing either would
        rewrite history behind an administrator who has already acted on it.
        """
        if 'requested_amount' not in vals or self._can_approve_recharge():
            return
        decided = self.filtered(lambda r: r.state != 'pending_approval')
        if decided:
            raise AccessError(_(
                "Recharge request %(names)s has already been reviewed, so the "
                "amount can no longer be changed. Raise a new recharge request "
                "instead.",
                names=', '.join(decided.mapped('name')),
            ))

    def _is_own_request(self):
        """Whether this request was raised by the user asking to act on it.

        Matched on the partner, which is how the portal controllers resolve a
        seller (``logistics.seller.user_id`` is a non-stored compute and cannot
        be searched). Read sudoed because a portal seller cannot necessarily
        read every field on the chain.
        """
        self.ensure_one()
        partner = self.env.user.partner_id
        return bool(partner) and self.sudo().seller_id.partner_id == partner

    def _check_may_cancel(self):
        """Let a seller withdraw a request they raised; nothing more.

        Withdrawing your own pending paperwork is reasonable and moves no
        money: a pending request has no wallet transaction behind it. Cancelling
        an *approved* request is a different act entirely — action_cancel
        unlinks the credit, which takes money back out of a wallet — so that
        stays with the administrators, as does cancelling anyone else's request.
        """
        self.ensure_one()
        if self._can_approve_recharge():
            return
        if self.state != 'pending_approval' or self.wallet_transaction_id:
            raise AccessError(_(
                "Recharge request %(name)s has already been reviewed and can "
                "no longer be withdrawn. Contact KeralaXpress support if it "
                "needs to be reversed.",
                name=self.name or '',
            ))
        if not self._is_own_request():
            raise AccessError(_(
                "You can only withdraw wallet recharge requests you raised "
                "yourself."
            ))

    def _get_logistics_admin_users(self):
        """Internal users in logistics admin group (excludes portal/public/system).

        Uses sudo for the group/user lookup: portal sellers cannot read
        res.groups, and this is only used to pick admin activity assignees.
        """
        admin_group = self.env.ref('keralariders_logistics.group_logistics_admin', raise_if_not_found=False)
        if not admin_group:
            return self.env['res.users']
        root_user = self.env.ref('base.user_root', raise_if_not_found=False)
        return admin_group.sudo().user_ids.filtered(
            lambda u: u.active and not u.share and (not root_user or u != root_user)
        )

    def _schedule_admin_approval_activities(self):
        """Create one To-Do activity per logistics admin for pending recharge requests."""
        try:
            admin_users = self._get_logistics_admin_users()
        except AccessError:
            _logger.warning(
                "Could not resolve logistics admin users for recharge activities "
                "(insufficient rights for %s); skipping activity schedule.",
                self.env.user.login,
                exc_info=True,
            )
            return self.env['mail.activity']
        if not admin_users:
            return self.env['mail.activity']
        activities = self.env['mail.activity']
        for request in self:
            amount = request.currency_id.format(request.requested_amount) if request.currency_id else request.requested_amount
            note = _(
                "Seller: %(seller)s<br/>"
                "Amount: %(amount)s<br/>"
                "Reference: %(reference)s",
                seller=request.seller_id.display_name or '',
                amount=amount,
                reference=request.name or '',
            )
            for user in admin_users:
                activities |= request.sudo().activity_schedule(
                    self._ACTIVITY_TYPE_TODO,
                    summary=_('Wallet recharge pending approval'),
                    note=note,
                    user_id=user.id,
                )
        return activities

    def _notify_admins_recharge_request(self):
        """Email / inbox notify logistics admins about a new wallet recharge request."""
        try:
            admin_users = self._get_logistics_admin_users()
        except AccessError:
            _logger.warning(
                "Could not resolve logistics admin users for recharge email "
                "(insufficient rights for %s); skipping mail.",
                self.env.user.login,
                exc_info=True,
            )
            return
        partners = admin_users.mapped('partner_id').filtered(lambda p: p.email)
        if not partners:
            return
        for request in self:
            amount = request.currency_id.format(request.requested_amount) if request.currency_id else request.requested_amount
            body = _(
                "<p>A seller submitted a wallet recharge request.</p>"
                "<ul>"
                "<li><strong>Seller:</strong> %(seller)s</li>"
                "<li><strong>Amount:</strong> %(amount)s</li>"
                "<li><strong>Reference:</strong> %(reference)s</li>"
                "</ul>"
                "<p>Please verify the payment and approve or cancel the request.</p>",
                seller=request.seller_id.display_name or '',
                amount=amount,
                reference=request.name or '',
            )
            request.sudo().message_notify(
                partner_ids=partners.ids,
                subject=_('Wallet recharge pending approval — %s') % (request.name or ''),
                body=body,
                email_layout_xmlid='mail.mail_notification_light',
            )

    def _complete_admin_approval_activities(self, feedback):
        """Mark open automated To-Do activities on these requests as done."""
        self.sudo().activity_feedback(
            [self._ACTIVITY_TYPE_TODO],
            feedback=feedback,
        )

    def action_approve_request(self):
        # Checked here and not only on the fields it writes: this is a public
        # method a portal seller can call over RPC on their own request, and
        # its only previous defence was portal lacking create rights on
        # logistics.wallet.transaction. That ACL protects a different model for
        # a different reason and could be loosened by an unrelated feature
        # tomorrow; crediting a wallet must not depend on it.
        self._check_recharge_admin(_(
            "Only a Logistics Administrator can approve a wallet recharge "
            "request. Your request will be credited once KeralaXpress has "
            "verified the payment."
        ))
        self.ensure_one()
        if self.recharged_amount <= 0:
            raise UserError(_('Recharge amount must be greater than 0.'))
        if not self.wallet_transaction_id:
            transaction = self.env['logistics.wallet.transaction'].create({
                'wallet_id': self.wallet_id.id,
                'amount': self.recharged_amount,
                'transaction_date': fields.Date.context_today(self),
                'reference': f'Recharge - {self.display_name}',
            })
            # One guarded write: the credit, the approval and the stamp of who
            # made it are the same decision and are recorded together.
            self.with_context(allow_recharge_approval_write=True).write({
                'approved_by': self.env.user.id,
                'approved_date': fields.Datetime.now(),
                'wallet_transaction_id': transaction.id,
                'state': 'approved',
            })
            self._complete_admin_approval_activities(_('Approved'))

    def action_cancel(self):
        for request in self:
            request._check_may_cancel()
            if request.wallet_transaction_id:
                request.wallet_transaction_id.unlink()
        self.with_context(allow_recharge_approval_write=True).write({
            'state': 'cancelled',
        })
        self._complete_admin_approval_activities(_('Cancelled'))

    def action_reset(self):
        # Reopening is not the counterpart of withdrawing: it puts the request
        # back in front of an administrator to be approved, so it belongs to
        # the same people who do the approving.
        self._check_recharge_admin(_(
            "Only a Logistics Administrator can reopen a wallet recharge "
            "request. Please raise a new request instead."
        ))
        self.with_context(allow_recharge_approval_write=True).write({
            'state': 'pending_approval',
        })
        self._schedule_admin_approval_activities()
        self._notify_admins_recharge_request()


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