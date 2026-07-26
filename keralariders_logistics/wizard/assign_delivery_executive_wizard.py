from odoo import models, fields, api
from odoo.exceptions import UserError

class AssignDeliveryExecutiveWizard(models.TransientModel):
    _name = 'logistics.assign.delivery.executive.wizard'
    _description = 'Assign Delivery Executive Wizard'

    delivery_executive_id = fields.Many2one('logistics.delivery.executive', string='Delivery Executive', required=True)
    shipment_ids = fields.Many2many('logistics.shipment', relation='logistics_assign_exec_shipment_rel', string='Shipments')

    @api.model
    def default_get(self, fields_list):
        res = super(AssignDeliveryExecutiveWizard, self).default_get(fields_list)
        active_ids = self.env.context.get('active_ids', [])
        if active_ids:
            res['shipment_ids'] = [(6, 0, active_ids)]
        return res

    def action_assign(self):
        for shipment in self.shipment_ids:
            if shipment.delivery_executive_id:
                raise UserError(f"Shipment {shipment.name} is already assigned to {shipment.delivery_executive_id.name}. Please select only unassigned shipments.")
            shipment.delivery_executive_id = self.delivery_executive_id.id
        return {'type': 'ir.actions.act_window_close'}
