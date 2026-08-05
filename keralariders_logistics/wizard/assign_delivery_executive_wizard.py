from odoo import models, fields, api, _
from odoo.exceptions import UserError


class AssignDeliveryExecutiveWizard(models.TransientModel):
    _name = 'logistics.assign.delivery.executive.wizard'
    _description = 'Assign Delivery Executive Wizard'

    role_filter = fields.Selection([
        ('any', 'Any Role'),
        ('pickup', 'Pickup'),
        ('delivery', 'Delivery'),
        ('driver', 'Hub-to-Hub Driver'),
        ('manager', 'Manager'),
    ], string='Role Filter', default='any')
    delivery_executive_id = fields.Many2one(
        'logistics.delivery.executive',
        string='Delivery Executive',
        required=True,
        domain="[('active', '=', True)]",
    )
    shipment_ids = fields.Many2many('logistics.shipment', relation='logistics_assign_exec_shipment_rel', string='Shipments')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        active_ids = self.env.context.get('active_ids', [])
        if active_ids:
            res['shipment_ids'] = [(6, 0, active_ids)]
        return res

    @api.onchange('role_filter')
    def _onchange_role_filter(self):
        domain = [('active', '=', True)]
        if self.role_filter == 'pickup':
            domain.append(('is_pickup', '=', True))
        elif self.role_filter == 'delivery':
            domain.append(('is_delivery', '=', True))
        elif self.role_filter == 'driver':
            domain.append(('is_driver', '=', True))
        elif self.role_filter == 'manager':
            domain.append(('is_manager', '=', True))
        return {'domain': {'delivery_executive_id': domain}}

    def action_assign(self):
        for shipment in self.shipment_ids:
            shipment._sync_active_leg()
            leg = shipment.active_leg_id or shipment._get_next_actionable_leg()
            if not leg:
                # Fallback: shipment-level assign only (no route legs yet)
                if shipment.delivery_executive_id and shipment.delivery_executive_id != self.delivery_executive_id:
                    raise UserError(
                        _("Shipment %s is already assigned to %s.")
                        % (shipment.name, shipment.delivery_executive_id.name)
                    )
                shipment.delivery_executive_id = self.delivery_executive_id.id
                if shipment.custodian_type in ('seller', 'de') and shipment.state not in ('delivered', 'cancelled', 'cancel'):
                    shipment.custodian_de_id = self.delivery_executive_id.id
                continue

            if leg.assigned_de_id and leg.assigned_de_id != self.delivery_executive_id and leg.state != 'planned':
                raise UserError(
                    _("Shipment %s leg '%s' is already assigned to %s. Clear the assignee first.")
                    % (shipment.name, leg.name, leg.assigned_de_id.name)
                )
            if not shipment._de_eligible_for_leg(self.delivery_executive_id, leg):
                raise UserError(
                    _("Delivery executive %s is not eligible for %s on shipment %s.")
                    % (self.delivery_executive_id.name, leg.operation_type, shipment.name)
                )

            # Assign on active leg without taking custody (hub dispatch / pickup still required)
            start = False
            shipment._assign_leg_de(leg, self.delivery_executive_id, start=start)
            shipment.delivery_executive_id = self.delivery_executive_id.id
            if shipment.custodian_type in ('seller', 'de') and shipment.state not in ('delivered', 'cancelled', 'cancel'):
                shipment.custodian_de_id = self.delivery_executive_id.id
            shipment._create_custody_event(
                'leg_assign',
                to_custodian=shipment.custodian_type,
                actor_de=self.delivery_executive_id,
                note=_("Assigned to %s for %s leg.") % (
                    self.delivery_executive_id.name, leg.operation_type
                ),
                leg=leg,
            )
        return {'type': 'ir.actions.act_window_close'}
