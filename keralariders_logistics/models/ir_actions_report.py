"""Print AWB is a single QWeb page for every fulfilment method.

India Post shipments use ``report_shipment_document_indiapost``. The official
CEPT sticker stays on the separate Print India Post Label action and is not
merged onto this report.
"""

from odoo import api, models, _
from odoo.exceptions import AccessError

SHIPMENT_AWB_REPORT = 'keralariders_logistics.report_shipment_document'


class IrActionsReport(models.Model):
    _inherit = 'ir.actions.report'

    def _kx_portal_forbid_draft_awb(self, report_ref, res_ids):
        """Sellers cannot download AWB PDFs for unbooked / draft shipments."""
        if not res_ids or not self.env.user.share:
            return
        try:
            report = self._get_report(report_ref)
        except Exception:
            return
        if report.report_name != SHIPMENT_AWB_REPORT:
            return
        seller = self.env['logistics.seller'].sudo().search(
            [('partner_id', '=', self.env.user.partner_id.id)], limit=1)
        if not seller:
            return
        shipments = self.env['logistics.shipment'].browse(res_ids).exists()
        if shipments.filtered(lambda s: not s.portal_awb_printable()):
            raise AccessError(_(
                'AWB labels are available after you request pickup.'
            ))

    def _render_qweb_pdf_prepare_streams(self, report_ref, data, res_ids=None):
        self._kx_portal_forbid_draft_awb(report_ref, res_ids)
        collected = super()._render_qweb_pdf_prepare_streams(
            report_ref, data, res_ids)
        try:
            report = self._get_report(report_ref)
        except Exception:
            return collected
        if report.report_name != SHIPMENT_AWB_REPORT:
            return collected
        return self._ip_append_indiapost_labels(collected)

    @api.model
    def _render_qweb_html(self, report_ref, docids, data=None):
        self._kx_portal_forbid_draft_awb(report_ref, docids)
        return super()._render_qweb_html(report_ref, docids, data=data)

    def _ip_append_indiapost_labels(self, collected_streams):
        """Identity: Print AWB must not grow a second CEPT page."""
        return collected_streams
