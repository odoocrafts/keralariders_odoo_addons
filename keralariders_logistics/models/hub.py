from odoo import models,fields,api
from odoo.exceptions import UserError

class Hub(models.Model):
    _name = "logistics.hub"

    name = fields.Char(string="Hub Name")
    district_id = fields.Many2one('logistics.district', string="District", required=True)

    @api.model
    def get_hub_from_pincode(self, pincode):
        # Return the first hub belonging to that district. In future with new hubs, add reroute logic for best hub selection
        hub = self.env['logistics.hub'].search([('pincode_ids', 'in', [pincode])], limit=1)
        if hub:
            return hub
        else:
            return hub
            raise UserError(f'Cannot find any Hub assigned to the selected Pincode or District')

    pincode_ids = fields.Many2many('logistics.pincode', string="Assigned Pincodes")
    pincodes_count = fields.Integer(string="Pincodes Count", compute="_compute_pincodes_count")

    def _compute_pincodes_count(self):
        for rec in self:
            rec.pincodes_count = len(rec.pincode_ids)