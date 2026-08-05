from markupsafe import Markup
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ShipmentInherit(models.Model):
    _inherit = "logistics.shipment"

    estimated_route_ids = fields.One2many(
        'logistics.shipment.estimated.route',
        'shipment_id',
        compute="_compute_estimated_route_ids",
        store=True,
    )
    estimated_route_html = fields.Html(compute="_compute_estimated_route_html")
    source_hub_id = fields.Many2one(
        'logistics.hub',
        string="Source Hub",
        compute="_compute_estimated_route_ids",
        store=True,
    )
    destination_hub_id = fields.Many2one(
        'logistics.hub',
        string="Destination Hub",
        compute="_compute_estimated_route_ids",
        store=True,
    )

    @api.depends('shipping_from_zip', 'shipping_to_zip')
    def _compute_estimated_route_ids(self):
        """Plan route: same hub = pickup→delivery; different hubs = pickup→hub_transfer→delivery.

        Cross-district / north↔south is a direct source→dest hub transfer.
        Thrissur main hub is NOT inserted automatically; optional central_pass_through
        is recorded as an event when the physical path crosses that area.
        """
        for rec in self:
            # Preserve planned route after ops have started
            if rec.route_locked or (rec.id and rec.event_ids):
                rec.estimated_route_ids = rec.estimated_route_ids
                rec.source_hub_id = rec.source_hub_id
                rec.destination_hub_id = rec.destination_hub_id
                continue
            if not (rec.shipping_from_zip and rec.shipping_to_zip):
                rec.estimated_route_ids = [(5, 0, 0)]
                rec.source_hub_id = False
                rec.destination_hub_id = False
                continue

            source_hub = self.env['logistics.hub'].get_hub_from_pincode(rec.shipping_from_zip)
            final_hub = self.env['logistics.hub'].get_hub_from_pincode(rec.shipping_to_zip)
            DE = self.env['logistics.delivery.executive']

            pickup_exec = DE.get_assigned_executive_for_pincode(rec.shipping_from_zip, 'pickup')
            delivery_exec = DE.get_assigned_executive_for_pincode(rec.shipping_to_zip, 'delivery')

            if source_hub == final_hub:
                # Same hub / same district: pickup → delivery (2 legs)
                lines = [
                    (0, 0, {
                        'sequence': 1,
                        'name': f'Pickup from Source Address --> {source_hub.name}',
                        'source_location_name': 'Pickup from Source Address',
                        'destination_location_name': f'{source_hub.name}',
                        'from_hub_id': False,
                        'to_hub_id': source_hub.id,
                        'executive1_id': pickup_exec.id if pickup_exec else False,
                        'operation_type': 'pickup',
                        'state': 'planned',
                    }),
                    (0, 0, {
                        'sequence': 2,
                        'name': f'{source_hub.name} --> Delivery at Consignee Address',
                        'source_location_name': f'{source_hub.name}',
                        'destination_location_name': 'Delivery at Consignee Address',
                        'from_hub_id': source_hub.id,
                        'to_hub_id': False,
                        'executive1_id': delivery_exec.id if delivery_exec else False,
                        'operation_type': 'delivery',
                        'state': 'planned',
                    }),
                ]
            else:
                # Different hubs (any districts/zones): direct source hub → dest hub
                lines = [
                    (0, 0, {
                        'sequence': 1,
                        'name': f'Pickup from Source Address --> {source_hub.name}',
                        'source_location_name': 'Pickup from Source Address',
                        'destination_location_name': f'{source_hub.name}',
                        'from_hub_id': False,
                        'to_hub_id': source_hub.id,
                        'executive1_id': pickup_exec.id if pickup_exec else False,
                        'operation_type': 'pickup',
                        'state': 'planned',
                    }),
                    (0, 0, {
                        'sequence': 2,
                        'name': f'{source_hub.name} --> {final_hub.name}',
                        'source_location_name': f'{source_hub.name}',
                        'destination_location_name': f'{final_hub.name}',
                        'from_hub_id': source_hub.id,
                        'to_hub_id': final_hub.id,
                        'operation_type': 'hub_transfer',
                        'state': 'planned',
                    }),
                    (0, 0, {
                        'sequence': 3,
                        'name': f'{final_hub.name} --> Delivery at Consignee Address',
                        'source_location_name': f'{final_hub.name}',
                        'destination_location_name': 'Delivery at Consignee Address',
                        'from_hub_id': final_hub.id,
                        'to_hub_id': False,
                        'executive1_id': delivery_exec.id if delivery_exec else False,
                        'operation_type': 'delivery',
                        'state': 'planned',
                    }),
                ]

            rec.estimated_route_ids = [(5, 0, 0)] + lines
            rec.source_hub_id = source_hub.id
            rec.destination_hub_id = final_hub.id

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
                        Planned Journey
                    </h3>
                </div>
                """
            ]

            routes = rec.estimated_route_ids.sorted("sequence")

            operation_type_map = {
                'pickup': ('Pickup', '#059669'),
                'hub_transfer': ('Hub Transfer', '#0891b2'),
                'delivery': ('Delivery', '#7c3aed'),
            }

            for index, route in enumerate(routes, start=1):
                if not route.from_hub_id:
                    badge_bg = "#10b981"
                    badge_color = "white"
                    icon = ""
                    from_name = route.source_location_name or "Pickup Address"
                else:
                    badge_bg = "#0ea5e9"
                    badge_color = "white"
                    icon = ""
                    from_name = route.source_location_name or route.from_hub_id.name

                if not route.to_hub_id:
                    to_name = route.destination_location_name or "Delivery Address"
                    to_icon = ""
                    to_badge_bg = "#f59e0b"
                else:
                    to_name = route.destination_location_name or route.to_hub_id.name
                    to_icon = ""
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
                        {"<div style='position:absolute;left:19px;top:40px;width:2px;height:60px;background:linear-gradient(to bottom, #3b82f6, #d1d5db);'></div>" if index != len(routes) else ""}
                        <div style="
                            flex:1;
                            background:white;
                            border:1.5px solid #e0e7ff;
                            border-radius:12px;
                            padding:16px;
                            box-shadow:0 4px 12px rgba(0,0,0,.06);
                        ">
                            <div style="margin-bottom:12px;">
                                <span style="
                                    color:black;
                                    padding:6px 10px;
                                    font-weight:900;
                                    font-size:11px;
                                    white-space:nowrap;
                                    text-transform:uppercase;
                                ">
                                    {operation_type_label}
                                </span>
                            </div>
                            <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
                                <span style="
                                    background:{badge_bg};color:{badge_color};
                                    padding:8px 12px;border-radius:20px;font-weight:600;font-size:13px;
                                ">{icon} {from_name}</span>
                                <i class="fa fa-arrow-right" style="color:#3b82f6; font-size:16px;"></i>
                                <span style="
                                    background:{to_badge_bg};color:white;
                                    padding:8px 12px;border-radius:20px;font-weight:600;font-size:13px;
                                ">{to_icon} {to_name}</span>
                            </div>
                            <div style="display:flex;gap:12px;margin-top:12px;flex-wrap:wrap;">
                                <span style="background:#64748b;color:white;padding:6px 10px;border-radius:4px;font-weight:600;font-size:12px;">{route.state or 'planned'}</span>
                                {"<span style='background:#0f766e;color:white;padding:6px 10px;border-radius:4px;font-weight:600;font-size:12px;'>Assigned: " + route.assigned_de_id.name + "</span>" if route.assigned_de_id else ""}
                                {"<span style='background:#8b5cf6;color:white;padding:6px 10px;border-radius:4px;font-weight:600;font-size:12px;'>Suggested: " + route.executive1_id.name + "</span>" if route.executive1_id and not route.assigned_de_id else ""}
                                {"<span style='background:#475569;color:white;padding:6px 10px;border-radius:4px;font-weight:600;font-size:12px;'>Started: " + fields.Datetime.to_string(route.started_at) + "</span>" if route.started_at else ""}
                                {"<span style='background:#15803d;color:white;padding:6px 10px;border-radius:4px;font-weight:600;font-size:12px;'>Done: " + fields.Datetime.to_string(route.completed_at) + "</span>" if route.completed_at else ""}
                            </div>
                        </div>
                    </div>
                """)

            html.append("</div>")
            rec.estimated_route_html = Markup("".join(html))

    def action_recompute_estimated_route(self):
        """Recompute planned route. Admins may force unlock; otherwise blocked after ops start."""
        is_admin = self.env.user.has_group('keralariders_logistics.group_logistics_admin')
        for rec in self:
            if rec.route_locked or rec.event_ids:
                if not is_admin:
                    raise UserError(
                        "Estimated route is locked because operations have started. "
                        "Only a Logistics Administrator can force recompute."
                    )
                rec.route_locked = False
            # Clear stored lines then recompute
            rec.estimated_route_ids = [(5, 0, 0)]
            rec._compute_estimated_route_ids()
            rec._sync_active_leg()


class ShipmentEstimatedRoute(models.Model):
    _name = "logistics.shipment.estimated.route"
    _description = "Shipment Estimated Route Leg"
    _order = "sequence asc, create_date asc"

    sequence = fields.Integer()
    shipment_id = fields.Many2one('logistics.shipment', ondelete='cascade', index=True)
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
        ('delivery', 'Delivery'),
    ], string="Operation Type", default='pickup', index=True)

    state = fields.Selection([
        ('planned', 'Planned'),
        ('assigned', 'Assigned'),
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
        ('skipped', 'Skipped'),
    ], string="Leg State", default='planned', required=True, index=True)

    # Actual operational assignee (preferred over executive1/2 suggestions)
    assigned_de_id = fields.Many2one(
        'logistics.delivery.executive',
        string="Assigned DE",
        index=True,
        help="Delivery executive currently assigned to execute this leg.",
    )
    started_at = fields.Datetime(string="Started At")
    completed_at = fields.Datetime(string="Completed At")

    # Suggested executives from route planning (not operational assignee)
    executive1_id = fields.Many2one('logistics.delivery.executive', string="Suggested DE")
    executive2_id = fields.Many2one('logistics.delivery.executive', string="Suggested DE 2")

    def action_clear_assignee(self):
        """Clear leg assignee so another DE can be assigned / self-assign."""
        for leg in self:
            if leg.state in ('done', 'skipped', 'in_progress'):
                raise UserError(
                    _("Cannot clear assignee on leg '%s' in state '%s'.")
                    % (leg.name, leg.state)
                )
            leg.write({
                'assigned_de_id': False,
                'state': 'planned',
                'started_at': False,
            })
        return True
