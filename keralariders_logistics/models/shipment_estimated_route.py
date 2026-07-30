from markupsafe import Markup
from odoo import models, fields, api
from odoo.exceptions import UserError
from .pincode_district import south_kerala_districts, central_district, north_kerala_districts

class ShipmentInherit(models.Model):
    _inherit = "logistics.shipment"
    estimated_route_ids = fields.One2many('logistics.shipment.estimated.route', 'shipment_id', compute="_compute_estimated_route_ids", store=True)
    estimated_route_html = fields.Html(compute="_compute_estimated_route_html")
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
                            'source_location_name': 'Pickup from Source Address',
                            'destination_location_name': f'{source_hub.name}',
                            'from_hub_id': False,
                            'to_hub_id': source_hub.id,
                            'executive1_id': self.env['logistics.delivery.executive'].get_assigned_executive_for_pincode(rec.shipping_from_zip, 'pickup').id,
                            'operation_type': 'pickup',
                        }),(0, 0, {
                            'sequence': 2,
                            'name': f'{source_hub.name} --> Delivery at Consignee Address',
                            'source_location_name': f'{source_hub.name}',
                            'destination_location_name': f'Delivery at Consignee Address',
                            'from_hub_id': source_hub.id,
                            'to_hub_id': False,
                            'executive1_id': self.env['logistics.delivery.executive'].get_assigned_executive_for_pincode(rec.shipping_to_zip, 'delivery').id,
                            'operation_type': 'delivery',
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
                            'source_location_name': 'Pickup from Source Address',
                            'destination_location_name': f'{source_hub.name}',
                            'from_hub_id': False,
                            'to_hub_id': source_hub.id,
                            'executive1_id': self.env['logistics.delivery.executive'].get_assigned_executive_for_pincode(rec.shipping_from_zip, 'pickup').id,
                            'operation_type': 'pickup',
                        }),(0, 0, {
                            'sequence': 2,
                            'name': f'{source_hub.name} --> {final_hub.name}',
                            'source_location_name': f'{source_hub.name}',
                            'destination_location_name': f'{final_hub.name}',
                            'from_hub_id': source_hub.id,
                            'to_hub_id': final_hub.id,
                            'operation_type': 'hub_transfer',
                        }),(0, 0, {
                            'sequence': 3,
                            'name': f'{final_hub.name} --> Delivery at Consignee Address',
                            'source_location_name': f'{final_hub.name}',
                            'destination_location_name': f'Delivery at Consignee Address',
                            'from_hub_id': final_hub.id,
                            'to_hub_id': False,
                            'operation_type': 'delivery',
                            'executive1_id': self.env['logistics.delivery.executive'].get_assigned_executive_for_pincode(rec.shipping_to_zip, 'delivery').id,
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
                        'source_location_name': 'Pickup from Source Address',
                        'destination_location_name': f'{source_hub.name}',
                        'from_hub_id': False,
                        'to_hub_id': source_hub.id,
                        'executive1_id': self.env['logistics.delivery.executive'].get_assigned_executive_for_pincode(rec.shipping_from_zip, 'pickup').id,
                        'operation_type': 'pickup',
                    }),(0, 0, {
                        'sequence': 4,
                        'name': f'{final_hub.name} --> Delivery at Consignee Address',
                        'source_location_name': f'{final_hub.name}',
                        'destination_location_name': f'Delivery at Consignee Address',
                        'from_hub_id': final_hub.id,
                        'to_hub_id': False,
                        'operation_type': 'delivery',
                        'executive1_id': self.env['logistics.delivery.executive'].get_assigned_executive_for_pincode(rec.shipping_to_zip, 'delivery').id,
                    }
                    )]
                    # If central_hub is the source or final hub, then the intermediary transfer is not required
                    if central_hub in (source_hub, final_hub):
                        lines.insert(1, (0, 0, {
                        'sequence': 2,
                        'name': f'{source_hub.name} --> {final_hub.name}',
                        'source_location_name': f'{source_hub.name}',
                        'destination_location_name': f'{final_hub.name}',
                        'from_hub_id': source_hub.id,
                        'to_hub_id': final_hub.id,
                        'operation_type': 'hub_transfer',
                    }))
                    # Add the Source to Central Hub line only when the Central HUB is not the actual Source or Destination Hub
                    else:
                        lines.insert(1, (0, 0, {
                        'sequence': 2,
                        'name': f'{source_hub.name} --> {central_hub.name}',
                        'source_location_name': f'{source_hub.name}',
                        'destination_location_name': f'{central_hub.name}',
                        'from_hub_id': source_hub.id,
                        'to_hub_id': central_hub.id,
                        'operation_type': 'hub_transfer',
                    }))
                        lines.insert(2, (0, 0, {
                        'sequence': 3,
                        'name': f'{central_hub.name} --> {final_hub.name}',
                        'source_location_name': f'{central_hub.name}',
                        'destination_location_name': f'{final_hub.name}',
                        'from_hub_id': central_hub.id,
                        'to_hub_id': final_hub.id,
                        'operation_type': 'hub_transfer',
                    }))
                rec.estimated_route_ids = [(2, route_id.id) for route_id in rec.estimated_route_ids] + lines                    


    @api.depends(
        'estimated_route_ids',
        'estimated_route_ids.sequence',
        'estimated_route_ids.name',
        'estimated_route_ids.from_hub_id',
        'estimated_route_ids.to_hub_id',
        'estimated_route_ids.operation_type',
    )
    def _compute_estimated_route_html(self):
        for rec in self:
            if not rec.estimated_route_ids:
                rec.estimated_route_html = False
                continue

            html = [
                """
                <div style="
                    background:linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
                    border:2px solid #3b82f6;
                    border-radius:14px;
                    padding:24px;
                    font-family:'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                ">
                <div style="margin-bottom:20px;">
                    <h3 style="margin:0; color:#1e3a8a; font-size:16px; font-weight:700;">
                        📍 Planned Journey
                    </h3>
                </div>
                """
            ]

            routes = rec.estimated_route_ids.sorted("sequence")

            operation_type_map = {
                'pickup': ('🎯 Pickup', '#059669'),
                'hub_transfer': ('🔄 Hub Transfer', '#0891b2'),
                'delivery': ('🚚 Delivery', '#7c3aed'),
            }

            for index, route in enumerate(routes, start=1):

                if not route.from_hub_id:
                    badge_bg = "#10b981"
                    badge_color = "white"
                    icon = "📍"
                    from_name = route.source_location_name or "Pickup Address"
                else:
                    badge_bg = "#0ea5e9"
                    badge_color = "white"
                    icon = "🏭"
                    from_name = route.source_location_name or route.from_hub_id.name

                if not route.to_hub_id:
                    to_name = route.destination_location_name or "Delivery Address"
                    to_icon = "🏠"
                    to_badge_bg = "#f59e0b"
                else:
                    to_name = route.destination_location_name or route.to_hub_id.name
                    to_icon = "🏢"
                    to_badge_bg = "#0ea5e9"

                operation_type_label, operation_type_color = operation_type_map.get(
                    route.operation_type, ('Unknown', '#6b7280')
                )

                html.append(f"""
                    <div style="
                        display:flex;
                        align-items:stretch;
                        margin-bottom:20px;
                        position:relative;
                    ">
                        <!-- Step Circle -->
                        <div style="
                            width:40px;
                            height:40px;
                            min-width:40px;
                            border-radius:50%;
                            background:linear-gradient(135deg, #3b82f6 0%, #0ea5e9 100%);
                            color:white;
                            font-weight:bold;
                            display:flex;
                            align-items:center;
                            justify-content:center;
                            font-size:16px;
                            margin-right:16px;
                            box-shadow:0 4px 12px rgba(59, 130, 246, 0.3);
                        ">
                            {index}
                        </div>

                        <!-- Connector Line -->
                        {"<div style='position:absolute;left:19px;top:40px;width:2px;height:60px;background:linear-gradient(to bottom, #3b82f6, #d1d5db);'></div>" if index != len(routes) else ""}

                        <!-- Content Card -->
                        <div style="
                            flex:1;
                            background:white;
                            border:1.5px solid #e0e7ff;
                            border-radius:12px;
                            padding:16px;
                            box-shadow:0 4px 12px rgba(0,0,0,.06);
                            transition:all 0.3s ease;
                        ">
                            <div style="
                                display:flex;
                                align-items:center;
                                gap:12px;
                                flex-wrap:wrap;
                                margin-bottom:12px;
                            ">
                                <!-- Operation Type Badge -->
                                <span style="
                                    # background:{operation_type_color};
                                    color:black;
                                    padding:6px 10px;
                                    # border-radius:16px;
                                    font-weight:900;
                                    font-size:11px;
                                    white-space:nowrap;
                                    box-shadow:0 2px 8px rgba(0,0,0,0.15);
                                    text-transform:uppercase;
                                ">
                                    {operation_type_label}
                                </span>
                            </div>
                            <div style="
                                display:flex;
                                align-items:center;
                                gap:12px;
                                flex-wrap:wrap;
                            ">
                                <!-- From Badge -->
                                <span style="
                                    background:{badge_bg};
                                    color:{badge_color};
                                    padding:8px 12px;
                                    border-radius:20px;
                                    font-weight:600;
                                    font-size:13px;
                                    white-space:nowrap;
                                    box-shadow:0 2px 8px rgba(16, 185, 129, 0.2);
                                ">
                                    {icon} {from_name}
                                </span>

                                <!-- Arrow -->
                                <div style="
                                    display:flex;
                                    align-items:center;
                                    gap:8px;
                                    color:#6b7280;
                                    font-size:12px;
                                    font-weight:600;
                                ">
                                    <i class="fa fa-arrow-right" style="color:#3b82f6; font-size:16px;"></i>
                                </div>

                                <!-- To Badge -->
                                <span style="
                                    background:{to_badge_bg};
                                    color:white;
                                    padding:8px 12px;
                                    border-radius:20px;
                                    font-weight:600;
                                    font-size:13px;
                                    white-space:nowrap;
                                    box-shadow:0 2px 8px rgba(15, 165, 233, 0.2);
                                ">
                                    {to_icon} {to_name}
                                </span>
                            </div>
                            <div style="
                                display:flex;
                                gap:12px;
                                margin-top:12px;
                                flex-wrap:wrap;
                            ">
                                {"<span style='background:#8b5cf6;color:white;padding:6px 10px;border-radius:16px;font-weight:600;font-size:12px;white-space:nowrap;box-shadow:0 2px 8px rgba(139, 92, 246, 0.2);'>👤 " + route.executive1_id.name + "</span>" if route.executive1_id else ""}
                                {"<span style='background:#ec4899;color:white;padding:6px 10px;border-radius:16px;font-weight:600;font-size:12px;white-space:nowrap;box-shadow:0 2px 8px rgba(236, 72, 153, 0.2);'>👤 " + route.executive2_id.name + "</span>" if route.executive2_id else ""}
                            </div>
                        </div>
                    </div>
                """)

            html.append("</div>")

            rec.estimated_route_html = Markup("".join(html))

    def action_recompute_estimated_route(self):
        for rec in self:
            rec._compute_estimated_route_ids()

class ShipmentEstimatedRoute(models.Model):
    _name = "logistics.shipment.estimated.route"
    _order = "sequence asc, create_date asc"
    sequence = fields.Integer()
    shipment_id = fields.Many2one('logistics.shipment')
    name = fields.Char(string="Route")
    source_location_name = fields.Char()
    destination_location_name = fields.Char()
    from_hub_id = fields.Many2one('logistics.hub', string="From HUB")
    from_district_id = fields.Many2one('logistics.district')
    to_hub_id = fields.Many2one('logistics.hub', string="To HUB")
    to_district_id = fields.Many2one('logistics.district')

    operation_type = fields.Selection([
        ('pickup', 'Pickup'),
        ('hub_transfer', 'Hub Transfer'),
        ('delivery', 'Delivery')
    ], string="Operation Type", default='pickup')

    executive1_id = fields.Many2one('logistics.delivery.executive', string="Executive 1")
    executive2_id = fields.Many2one('logistics.delivery.executive', string="Executive 2")

