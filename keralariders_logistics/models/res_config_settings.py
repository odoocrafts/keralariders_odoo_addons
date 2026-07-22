from odoo import models, fields

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    logistics_upi_id = fields.Char(related='company_id.logistics_upi_id', readonly=False, string="Logistics UPI ID")
