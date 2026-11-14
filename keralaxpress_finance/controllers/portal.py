from odoo import fields, http, _
from odoo.http import content_disposition, request

from odoo.addons.keralariders_logistics.controllers.portal import LogisticsPortal
from odoo.addons.keralariders_logistics.models.shipment import delivery_states

XLSX_MIMETYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
STATE_KEYS = {key for key, _label in delivery_states}


class KxFinancePortal(LogisticsPortal):
    """Seller-facing reports, ledger and invoices.

    The seller is always the one linked to the logged-in partner. Data is
    read with sudo, and every query is restricted to that seller here, so no
    request parameter can widen what a seller sees.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _kx_current_seller(self):
        return request.env['logistics.seller'].sudo().search(
            [('partner_id', '=', request.env.user.partner_id.id)], limit=1)

    @staticmethod
    def _kx_date_param(value, default=False):
        if not value:
            return default
        try:
            return fields.Date.to_date(value)
        except (TypeError, ValueError):
            return default

    def _kx_period(self, date_from, date_to):
        today = fields.Date.context_today(request.env.user)
        start = self._kx_date_param(date_from, today.replace(day=1))
        end = self._kx_date_param(date_to, today)
        if start and end and start > end:
            start, end = end, start
        return start, end

    @staticmethod
    def _kx_file_response(content, filename, mimetype):
        return request.make_response(content, headers=[
            ('Content-Type', mimetype),
            ('Content-Length', len(content)),
            ('Content-Disposition', content_disposition(filename)),
        ])

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if request.env.user._is_public():
            return values
        seller = self._kx_current_seller()
        if seller:
            count = request.env['kx.seller.invoice'].sudo().search_count([
                ('seller_id', '=', seller.id), ('state', '=', 'posted')])
            values['kx_invoice_count'] = str(count) if count else '0 '
        return values

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------
    @http.route(['/my/reports'], type='http', auth='user', website=True)
    def portal_kx_reports(self, date_from=None, date_to=None, state=None, **kw):
        seller = self._kx_current_seller()
        if not seller:
            return request.redirect('/my')
        start, end = self._kx_period(date_from, date_to)
        state = state if state in STATE_KEYS else ''
        service = request.env['kx.report.service'].sudo()
        orders = service.order_report(start, end, [seller.id], [state] if state else None)
        cod = service.cod_report(start, end, [seller.id])
        cod_now = request.env['logistics.account.transfer'].sudo() \
            .get_seller_cod_pending_balance(seller)
        values = {
            'page_name': 'kx_reports',
            'seller': seller,
            'date_from': start,
            'date_to': end,
            'state': state,
            'state_options': delivery_states,
            'orders': orders,
            'cod': cod,
            'cod_now': cod_now,
            'currency': seller.currency_id or request.env.company.currency_id,
            'export_query': {
                'date_from': fields.Date.to_string(start),
                'date_to': fields.Date.to_string(end),
                'state': state,
            },
        }
        return request.render('keralaxpress_finance.portal_my_kx_reports', values)

    @http.route(['/my/reports/export'], type='http', auth='user', website=True)
    def portal_kx_reports_export(self, report='orders', date_from=None, date_to=None,
                                 state=None, **kw):
        seller = self._kx_current_seller()
        if not seller:
            return request.redirect('/my')
        start, end = self._kx_period(date_from, date_to)
        service = request.env['kx.report.service'].sudo()
        if report == 'cod':
            content = service.cod_report_xlsx(
                start, end, [seller.id], include_seller_sheet=False)
            filename = 'COD_Report_%s_%s.xlsx' % (start, end)
        else:
            states = [state] if state in STATE_KEYS else None
            content = service.order_report_xlsx(
                start, end, [seller.id], states, include_seller_sheet=False)
            filename = 'Order_Report_%s_%s.xlsx' % (start, end)
        return self._kx_file_response(content, filename, XLSX_MIMETYPE)

    # ------------------------------------------------------------------
    # Ledger
    # ------------------------------------------------------------------
    @http.route(['/my/ledger'], type='http', auth='user', website=True)
    def portal_kx_ledger(self, date_from=None, date_to=None, source=None, **kw):
        seller = self._kx_current_seller()
        if not seller:
            return request.redirect('/my')
        start, end = self._kx_period(date_from, date_to)
        source = source if source in ('wallet', 'cod') else ''
        statement = request.env['kx.report.service'].sudo().ledger_statement(
            seller, start, end, source or None)
        values = {
            'page_name': 'kx_ledger',
            'seller': seller,
            'date_from': start,
            'date_to': end,
            'source': source,
            'statement': statement,
            'currency': statement['currency'],
            'export_query': {
                'date_from': fields.Date.to_string(start),
                'date_to': fields.Date.to_string(end),
                'source': source,
            },
        }
        return request.render('keralaxpress_finance.portal_my_kx_ledger', values)

    @http.route(['/my/ledger/export'], type='http', auth='user', website=True)
    def portal_kx_ledger_export(self, file_format='xlsx', date_from=None, date_to=None,
                                source=None, **kw):
        seller = self._kx_current_seller()
        if not seller:
            return request.redirect('/my')
        start, end = self._kx_period(date_from, date_to)
        source = source if source in ('wallet', 'cod') else None
        if file_format == 'pdf':
            pdf, _type = request.env['ir.actions.report'].sudo()._render_qweb_pdf(
                'keralaxpress_finance.action_report_ledger_statement', [seller.id], data={
                    'date_from': fields.Date.to_string(start),
                    'date_to': fields.Date.to_string(end),
                    'source': source or False,
                })
            return self._kx_file_response(
                pdf, 'Ledger_%s_%s.pdf' % (start, end), 'application/pdf')
        content = request.env['kx.report.service'].sudo().ledger_statement_xlsx(
            seller, start, end, source)
        return self._kx_file_response(
            content, 'Ledger_%s_%s.xlsx' % (start, end), XLSX_MIMETYPE)

    # ------------------------------------------------------------------
    # Invoices
    # ------------------------------------------------------------------
    # Not /my/invoices: Odoo Invoicing owns that route if it is ever installed.
    @http.route(['/my/kx_invoices'], type='http', auth='user', website=True)
    def portal_kx_invoices(self, **kw):
        seller = self._kx_current_seller()
        if not seller:
            return request.redirect('/my')
        invoices = request.env['kx.seller.invoice'].sudo().search([
            ('seller_id', '=', seller.id), ('state', '=', 'posted'),
        ], order='date_from desc, id desc')
        return request.render('keralaxpress_finance.portal_my_kx_invoices', {
            'page_name': 'kx_invoices',
            'seller': seller,
            'invoices': invoices,
        })

    @http.route(['/my/kx_invoices/<int:invoice_id>/pdf'], type='http', auth='user', website=True)
    def portal_kx_invoice_pdf(self, invoice_id, **kw):
        seller = self._kx_current_seller()
        invoice = request.env['kx.seller.invoice'].sudo().browse(invoice_id).exists()
        if not seller or not invoice or invoice.seller_id != seller or invoice.state != 'posted':
            return request.redirect('/my/kx_invoices')
        pdf, _type = request.env['ir.actions.report'].sudo()._render_qweb_pdf(
            'keralaxpress_finance.action_report_seller_invoice', [invoice.id])
        filename = '%s.pdf' % invoice._report_filename().replace('/', '-')
        return self._kx_file_response(pdf, filename, 'application/pdf')
