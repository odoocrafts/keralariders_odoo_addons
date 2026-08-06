import logging

from odoo import models, fields, api, _
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

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
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].sudo().next_by_code('logistics.wallet.recharge.request') or _('New')
        records = super(WalletRechargeRequest, self).create(vals_list)
        records.filtered(lambda r: r.state == 'pending_approval')._schedule_admin_approval_activities()
        return records
    
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

    def _complete_admin_approval_activities(self, feedback):
        """Mark open automated To-Do activities on these requests as done."""
        self.sudo().activity_feedback(
            [self._ACTIVITY_TYPE_TODO],
            feedback=feedback,
        )

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
            self._complete_admin_approval_activities(_('Approved'))

    def action_cancel(self):
        if self.wallet_transaction_id:
            self.wallet_transaction_id.unlink()
        self.state = 'cancelled'
        self._complete_admin_approval_activities(_('Cancelled'))

    def action_reset(self):
        self.state = 'pending_approval'
        self._schedule_admin_approval_activities()


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