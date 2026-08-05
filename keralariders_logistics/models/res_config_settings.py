from odoo import models, fields, api


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    logistics_upi_id = fields.Char(
        string="Logistics UPI ID",
        config_parameter='keralariders_logistics.logistics_upi_id',
    )
    company_cod_account_id = fields.Many2one(
        'logistics.account',
        string="Company COD Settlement Account",
        domain="[('account_type', 'in', ('company', 'bank', 'cash'))]",
        help="Company account that receives hub banking and pays seller COD clearances.",
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        account_id = self.env['ir.config_parameter'].sudo().get_param(
            'keralariders_logistics.company_cod_account_id'
        )
        res['company_cod_account_id'] = int(account_id) if account_id else False
        return res

    def set_values(self):
        super().set_values()
        self.env['ir.config_parameter'].sudo().set_param(
            'keralariders_logistics.company_cod_account_id',
            self.company_cod_account_id.id if self.company_cod_account_id else '',
        )
