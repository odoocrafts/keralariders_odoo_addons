from odoo import api, fields, models

GST_PERCENT_PARAM = 'keralaxpress_finance.invoice_gst_percent'
SAC_CODE_PARAM = 'keralaxpress_finance.invoice_sac_code'
DEFAULT_GST_PERCENT = 18.0
DEFAULT_SAC_CODE = '996812'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    kx_invoice_gst_percent = fields.Float(
        string='Invoice GST (%)',
        digits=(5, 2),
        config_parameter=GST_PERCENT_PARAM,
        default=DEFAULT_GST_PERCENT,
        help='GST included in the monthly seller invoice amount. Enter 18 for '
             '18%. Posted invoices keep the rate they were issued with.',
    )
    kx_invoice_sac_code = fields.Char(
        string='Invoice SAC code',
        config_parameter=SAC_CODE_PARAM,
        default=DEFAULT_SAC_CODE,
        help='Services Accounting Code printed on seller invoices.',
    )

    @api.model
    def kx_invoice_tax_settings(self):
        """The GST percent and SAC code new invoices are issued with."""
        ICP = self.env['ir.config_parameter'].sudo()
        raw = ICP.get_param(GST_PERCENT_PARAM)
        try:
            percent = float(raw) if raw not in (None, False, '') else DEFAULT_GST_PERCENT
        except ValueError:
            percent = DEFAULT_GST_PERCENT
        sac = ICP.get_param(SAC_CODE_PARAM) or DEFAULT_SAC_CODE
        return max(percent, 0.0), sac
