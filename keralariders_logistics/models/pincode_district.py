from odoo import models, fields, api, _

class Pincode(models.Model):
    _name = 'logistics.pincode'
    _description = 'Pincode'

    name = fields.Char(string='Pincode')
    district_id = fields.Many2one('logistics.district', string='District')
    district_name = fields.Char(string='District Name', store=True)
    state_name = fields.Char(string='State Name', store=True)

class District(models.Model):
    _name = 'logistics.district'
    _order="name"
    _description = 'District'

    name = fields.Char(string='District Name')
    state_id = fields.Many2one('res.country.state', string='State')

    @api.model
    def get_district_from_pincode(self, pincode):
        """Get district and state from pincode."""
        self.env.cr.execute(
            "SELECT district_name, state_name FROM logistics_pincode WHERE name = %s LIMIT 1",
            (pincode,)
        )
        result = self.env.cr.fetchone()
        if result:

            district_name, state_name = result
            district_id = self.search([('name', 'ilike', district_name)], limit=1)
            return {
                'district_id': district_id,
                'district_name': district_name,
                'state_name': state_name,
            }
        return {
            'district_id': False,
            'district_name': '',
            'state_name': '',
        }