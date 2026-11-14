from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

from ..tools.xlsx import DATE, MONEY, TEXT, build_workbook

ADMIN_GROUP = 'keralariders_logistics.group_logistics_admin'

# What finance staff may still change on an expense an administrator has
# already decided. Anything else would rewrite the approved figures.
DECIDED_WRITABLE = {'message_main_attachment_id', 'attachment_ids'}


class ExpenseCategory(models.Model):
    _name = 'kx.expense.category'
    _description = 'Expense Category'
    _order = 'sequence, name'

    name = fields.Char(string='Category', required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    description = fields.Text(string='Description')

    _name_uniq = models.Constraint('UNIQUE(name)', 'This expense category already exists.')


class Expense(models.Model):
    _name = 'kx.expense'
    _description = 'Company Expense'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'

    name = fields.Char(string='Reference', readonly=True, copy=False, default='/')
    date = fields.Date(
        string='Date', required=True, default=fields.Date.context_today, tracking=True)
    category_id = fields.Many2one(
        'kx.expense.category', string='Category', required=True, tracking=True,
        ondelete='restrict')
    description = fields.Char(string='Description', required=True, tracking=True)
    payee_partner_id = fields.Many2one('res.partner', string='Paid To')
    payee_name = fields.Char(string='Paid To (Name)', help='Use when the payee is not a contact.')
    amount = fields.Monetary(string='Amount', required=True, tracking=True, currency_field='currency_id')
    currency_id = fields.Many2one(
        'res.currency', string='Currency', required=True,
        default=lambda self: self.env.company.currency_id)
    company_id = fields.Many2one(
        'res.company', string='Company', required=True, default=lambda self: self.env.company)
    payment_mode = fields.Selection(
        [('cash', 'Cash'), ('bank', 'Bank Transfer'), ('upi', 'UPI'), ('cheque', 'Cheque'),
         ('card', 'Card')],
        string='Payment Mode', required=True, default='cash', tracking=True)
    account_id = fields.Many2one(
        'logistics.account', string='Paid From Account',
        help='For information only. Recording an expense never posts to this account.')
    hub_id = fields.Many2one('logistics.hub', string='Hub')
    bill_reference = fields.Char(string='Bill / Voucher No.', tracking=True)
    notes = fields.Text(string='Notes')
    attachment_ids = fields.Many2many('ir.attachment', string='Bills & Receipts')
    state = fields.Selection(
        [('draft', 'Draft'), ('approved', 'Approved'), ('cancelled', 'Cancelled')],
        string='Status', default='draft', required=True, readonly=True,
        tracking=True, index=True, copy=False)
    approved_by = fields.Many2one('res.users', string='Approved By', readonly=True, copy=False)
    approved_on = fields.Datetime(string='Approved On', readonly=True, copy=False)
    payee_display = fields.Char(string='Payee', compute='_compute_payee_display')

    @api.depends('payee_partner_id', 'payee_name')
    def _compute_payee_display(self):
        for exp in self:
            exp.payee_display = exp.payee_partner_id.display_name or exp.payee_name or ''

    @api.constrains('amount')
    def _check_amount(self):
        for exp in self:
            if exp.amount <= 0:
                raise ValidationError(_("An expense amount must be greater than zero."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].sudo().next_by_code('kx.expense') or '/'
            vals.pop('state', None)
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.context.get('kx_expense_decision') and not self._is_admin():
            if 'state' in vals:
                raise AccessError(_("Only a Logistics Administrator can approve or cancel expenses."))
            changed = set(vals) - DECIDED_WRITABLE
            if changed and self.filtered(lambda e: e.state != 'draft'):
                raise AccessError(_(
                    "This expense has already been approved or cancelled. Ask a "
                    "Logistics Administrator to reset it to draft."))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_draft(self):
        if self.filtered(lambda e: e.state != 'draft'):
            raise UserError(_("Only draft expenses can be deleted."))

    def _is_admin(self):
        return self.env.su or self.env.user.has_group(ADMIN_GROUP)

    def _check_admin(self):
        if not self._is_admin():
            raise AccessError(_("Only a Logistics Administrator can approve or cancel expenses."))

    def action_approve(self):
        self._check_admin()
        for exp in self:
            if exp.state != 'draft':
                raise UserError(_("Only draft expenses can be approved."))
        self.with_context(kx_expense_decision=True).write({
            'state': 'approved',
            'approved_by': self.env.user.id,
            'approved_on': fields.Datetime.now(),
        })
        return True

    def action_cancel(self):
        self._check_admin()
        self.with_context(kx_expense_decision=True).write({'state': 'cancelled'})
        return True

    def action_reset_draft(self):
        self._check_admin()
        self.with_context(kx_expense_decision=True).write({
            'state': 'draft', 'approved_by': False, 'approved_on': False,
        })
        return True

    @api.model
    def expense_report_xlsx(self, date_from=None, date_to=None, category_ids=None,
                            states=('approved',), hub_ids=None):
        """Expense register with a running total, plus a category summary."""
        domain = []
        if date_from:
            domain.append(('date', '>=', date_from))
        if date_to:
            domain.append(('date', '<=', date_to))
        if category_ids:
            domain.append(('category_id', 'in', list(category_ids)))
        if hub_ids:
            domain.append(('hub_id', 'in', list(hub_ids)))
        if states:
            domain.append(('state', 'in', list(states)))
        expenses = self.search(domain, order='date asc, id asc')
        state_labels = dict(self._fields['state'].selection)
        mode_labels = dict(self._fields['payment_mode'].selection)
        meta = [
            (_('From'), fields.Date.to_string(fields.Date.to_date(date_from)) if date_from else _('Beginning')),
            (_('To'), fields.Date.to_string(fields.Date.to_date(date_to)) if date_to else _('Today')),
            (_('Status'), ', '.join(state_labels[s] for s in states) if states else _('All')),
            (_('Generated on'), fields.Datetime.to_string(fields.Datetime.now())),
        ]
        running = 0.0
        rows = []
        for exp in expenses:
            running += exp.amount
            rows.append([
                exp.date, exp.name, exp.description, exp.category_id.name or '',
                exp.payee_display, mode_labels.get(exp.payment_mode, ''),
                exp.hub_id.name or '', exp.bill_reference or '',
                state_labels.get(exp.state, ''), _('Debit'), exp.amount, running,
            ])
        by_category = {}
        for exp in expenses:
            by_category[exp.category_id.name or ''] = (
                by_category.get(exp.category_id.name or '', 0.0) + exp.amount)
        return build_workbook([{
            'name': _('Expenses'),
            'title': _('Company Expenses'),
            'meta': meta,
            'columns': [
                (_('Date'), DATE), (_('Reference'), TEXT), (_('Description'), TEXT),
                (_('Category'), TEXT), (_('Paid To'), TEXT), (_('Payment Mode'), TEXT),
                (_('Hub'), TEXT), (_('Bill No.'), TEXT), (_('Status'), TEXT),
                (_('Cr/Dr'), TEXT), (_('Amount'), MONEY), (_('Running Total'), MONEY),
            ],
            'rows': rows,
            'totals': ['', _('Total'), '', '', '', '', '', '', '', '', running, running],
        }, {
            'name': _('By Category'),
            'title': _('Expenses by Category'),
            'meta': meta,
            'columns': [(_('Category'), TEXT), (_('Amount'), MONEY)],
            'rows': [[name, amount] for name, amount in sorted(by_category.items())],
            'totals': [_('Total'), running],
        }])
