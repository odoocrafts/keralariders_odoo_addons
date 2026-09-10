"""Print AWB is a single QWeb page for every fulfilment method.

India Post shipments use ``report_shipment_document_indiapost``. The official
CEPT sticker stays on the separate Print India Post Label action and is not
merged onto this report.
"""

from odoo import models

SHIPMENT_AWB_REPORT = 'keralariders_logistics.report_shipment_document'


class IrActionsReport(models.Model):
    _inherit = 'ir.actions.report'

    def _render_qweb_pdf_prepare_streams(self, report_ref, data, res_ids=None):
        collected = super()._render_qweb_pdf_prepare_streams(
            report_ref, data, res_ids)
        try:
            report = self._get_report(report_ref)
        except Exception:
            return collected
        if report.report_name != SHIPMENT_AWB_REPORT:
            return collected
        return self._ip_append_indiapost_labels(collected)

    def _ip_append_indiapost_labels(self, collected_streams):
        """Identity: Print AWB must not grow a second CEPT page."""
        return collected_streams
