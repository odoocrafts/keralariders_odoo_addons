"""Merge the stored India Post label onto the KeralaXpress AWB PDF.

Print AWB (admin shipment, admin order delivery slips, seller portal) all go
through ``keralariders_logistics.report_shipment_document``. That QWeb picks
the hub layout or the India Post layout from ``fulfilment_method``. When the
report is rendered for an India Post shipment that already has a label, the
stored India Post PDF is still appended as page 2. Hub-network shipments stay
the single KeralaXpress page.
"""

import io
import logging

from odoo import models

_logger = logging.getLogger(__name__)

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
        Shipment = self.env['logistics.shipment']
        for res_id, stream_data in collected_streams.items():
            if not res_id:
                continue
            stream = stream_data.get('stream')
            if not stream:
                continue
            shipment = Shipment.browse(res_id)
            if not shipment.exists() or shipment.fulfilment_method != 'indiapost':
                continue
            try:
                kx_pdf = stream.getvalue()
            except Exception:
                _logger.warning(
                    'Print AWB stream for shipment %s could not be read',
                    res_id, exc_info=True)
                continue
            merged = shipment._ip_merge_awb_pdf(kx_pdf)
            if merged and merged != kx_pdf:
                stream_data['stream'] = io.BytesIO(merged)
        return collected_streams
