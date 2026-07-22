from odoo import models, fields

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    logistics_upi_id = fields.Char(string="Logistics UPI ID", config_parameter='keralariders_logistics.logistics_upi_id')
    logistics_upiqr_api_key = fields.Char(string="UPIQR API Key", config_parameter='keralariders_logistics.logistics_upiqr_api_key', default="apikey")
