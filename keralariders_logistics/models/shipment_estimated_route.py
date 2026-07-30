from odoo import models, fields, api
from odoo.exceptions import UserError
from .pincode_district import south_kerala_districts, central_district, north_kerala_districts

class ShipmentInherit(models.Model):
    _inherit = "logistics.shipment"
    estimated_route_ids = fields.One2many('logistics.shipment.estimated.route', 'shipment_id', compute="_compute_estimated_route_ids", store=True)

    @api.depends('shipping_from_zip', 'shipping_to_zip')
    def _compute_estimated_route_ids(self):
        for rec in self:
            if rec.shipping_from_zip and rec.shipping_to_zip:
                # Find the Initial HUB
                source_hub = self.env['logistics.hub'].get_hub_from_pincode(rec.shipping_from_zip)
                final_hub = self.env['logistics.hub'].get_hub_from_pincode(rec.shipping_to_zip)
                get_district_zone = self.env['logistics.district'].get_district_zone
                # Check If the source and final hub are in same zone
                if get_district_zone(source_hub.district_id) == get_district_zone(final_hub.district_id):
                    # If source and final hub is same
                    # then only two routes are required:
                    # 1. Pickup from source -> Source Hub
                    # 2. Source Hub -> Consignee Address
                    if source_hub == final_hub:
                        lines = [(0, 0, {
                            'sequence': 1,
                            'name': f'Pickup from Source Address --> {source_hub.name}',
                            'from_hub_id': False,
                            'to_hub_id': source_hub.id,
                        }),(0, 0, {
                            'sequence': 2,
                            'name': f'{source_hub.name} --> Delivery at Consignee Address',
                            'from_hub_id': source_hub.id,
                            'to_hub_id': False
                        }
                        )]
                    # If source and final hub is in same zone but in different districts, 
                    # then Three routes are required:
                    # 1. Pickup from source -> Source Hub
                    # 2. Source Hub -> Final Destination Hub
                    # 3. Final Destination Hub -> Consignee Address
                    else:
                        lines = [(0, 0, {
                            'sequence': 1,
                            'name': f'Pickup from Source Address --> {source_hub.name}',
                            'from_hub_id': False,
                            'to_hub_id': source_hub.id,
                        }),(0, 0, {
                            'sequence': 2,
                            'name': f'{source_hub.name} --> {final_hub.name}',
                            'from_hub_id': source_hub.id,
                            'to_hub_id': final_hub.id,
                        }),(0, 0, {
                            'sequence': 3,
                            'name': f'{final_hub.name} --> Delivery at Consignee Address',
                            'from_hub_id': final_hub.id,
                            'to_hub_id': False
                        }
                        )]

                # If source and final hub is in different zones, 
                # then Four routes are required as the package is stored in the Central Hub before moving to Final hub:
                # 1. Pickup from source -> Source Hub
                # 2. Source Hub -> Central HUB (Thrissur)
                # 3. Central Hub (Thrissur) -> Final Destination Hub
                # 4. Final Destination Hub -> Consignee Address
                else:
                    central_district_id = self.env['logistics.district'].get_central_district_id()
                    central_hub = self.env['logistics.hub'].search([('district_id', '=', central_district_id.id)], limit=1)
                    if not central_hub:
                        raise UserError(f'Cannot find any Central HUB defined in the system!')
                    
                    lines = [(0, 0, {
                        'sequence': 1,
                        'name': f'Pickup from Source Address --> {source_hub.name}',
                        'from_hub_id': False,
                        'to_hub_id': source_hub.id,
                    }),(0, 0, {
                        'sequence': 4,
                        'name': f'{final_hub.name} --> Delivery at Consignee Address',
                        'from_hub_id': final_hub.id,
                        'to_hub_id': False
                    }
                    )]
                    # If central_hub is the source or final hub, then the intermediary transfer is not required
                    if central_hub in (source_hub, final_hub):
                        lines.insert(1, (0, 0, {
                        'sequence': 2,
                        'name': f'{source_hub.name} --> {final_hub.name}',
                        'from_hub_id': source_hub.id,
                        'to_hub_id': final_hub.id,
                    }))
                    # Add the Source to Central Hub line only when the Central HUB is not the actual Source or Destination Hub
                    else:
                        lines.insert(1, (0, 0, {
                        'sequence': 2,
                        'name': f'{source_hub.name} --> {central_hub.name}',
                        'from_hub_id': source_hub.id,
                        'to_hub_id': central_hub.id,
                    }))
                        lines.insert(2, (0, 0, {
                        'sequence': 3,
                        'name': f'{central_hub.name} --> {final_hub.name}',
                        'from_hub_id': central_hub.id,
                        'to_hub_id': final_hub.id,
                    }))


                rec.estimated_route_ids = [(2, route_id.id) for route_id in rec.estimated_route_ids] + lines                    


class ShipmentEstimatedRoute(models.Model):
    _name = "logistics.shipment.estimated.route"
    _order = "sequence asc, create_date asc"
    sequence = fields.Integer()
    shipment_id = fields.Many2one('logistics.shipment')
    name = fields.Char(string="Route")
    from_hub_id = fields.Many2one('logistics.hub', string="From HUB")
    from_district_id = fields.Many2one('logistics.district')
    to_hub_id = fields.Many2one('logistics.hub', string="To HUB")
    to_district_id = fields.Many2one('logistics.district')

