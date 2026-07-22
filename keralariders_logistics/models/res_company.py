from odoo import models, fields

class ResCompany(models.Model):
    _inherit = 'res.company'

    logistics_upi_id = fields.Char(string="Logistics UPI ID")
