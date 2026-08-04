from odoo import models, fields, api, _
from odoo.exceptions import UserError

north_kerala_districts = {
    'kerala_district_1': 'kasargod',
    'kerala_district_2': 'kannur',
    'kerala_district_3': 'wayanad',
    'kerala_district_4': 'kozhikode',
    'kerala_district_5': 'malappuram',
    'kerala_district_6': 'palakkad',
}

central_district = {
    'kerala_district_7': 'thrissur',
}

south_kerala_districts = {
    'kerala_district_8': 'ernakulam',
    'kerala_district_9': 'idukki',
    'kerala_district_10': 'kottayam',
    'kerala_district_11': 'alappuzha',
    'kerala_district_12': 'pathanamthitta',
    'kerala_district_13': 'kollam',
    'kerala_district_14': 'thiruvananthapuram',
}
class Pincode(models.Model):
    _name = 'logistics.pincode'
    _description = 'Pincode'

    name = fields.Char(string='Pincode')
    district_id = fields.Many2one('logistics.district', string='District', compute="_compute_district_id")

    def _compute_district_id(self):
        for rec in self:
            district_id = self.env['logistics.district'].get_district_from_pincode(rec.name)['district_id']
            rec.district_id = district_id.id if district_id else False

    district_name = fields.Char(string='District Name', store=True)
    state_name = fields.Char(string='State Name', store=True)
    po_names = fields.Text(string='Post Office Names', store=True)

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
            # CSV uses KASARAGOD; seeded district is named Kasargod
            if not district_id and district_name and district_name.upper() == 'KASARAGOD':
                district_id = self.search([('name', 'ilike', 'Kasargod')], limit=1)
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

    @api.model
    def get_district_zone(self, district_id):
        district_name = (district_id.name or "").lower()
        if district_name in central_district.values():
            return 'central'
        elif district_name in north_kerala_districts.values():
            return 'north'
        elif district_name in south_kerala_districts.values():
            return 'south'
        # else:
        #     raise UserError(f'Zone cannot be determined from the {district_name} district')

    @api.model
    def get_central_district_id(self):
        central_district_id = self.env.ref(f'keralariders_logistics.{list(central_district.keys())[0]}')
        return central_district_id