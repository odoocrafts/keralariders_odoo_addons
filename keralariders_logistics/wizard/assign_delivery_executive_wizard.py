from odoo import models, fields, api
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
            if shipment.delivery_executive_id:
                raise UserError(
                    f"Shipment {shipment.name} is already assigned to "
                    f"{shipment.delivery_executive_id.name}. Please select only unassigned shipments."
                )
            shipment.delivery_executive_id = self.delivery_executive_id.id
            if shipment.custodian_type in ('seller', 'de') and shipment.state not in ('delivered', 'cancelled', 'cancel'):
                shipment.custodian_de_id = self.delivery_executive_id.id
        return {'type': 'ir.actions.act_window_close'}
