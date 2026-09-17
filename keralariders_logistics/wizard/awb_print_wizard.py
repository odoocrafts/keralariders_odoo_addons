from odoo import api, fields, models, _
from odoo.exceptions import UserError


class AwbPrintWizard(models.TransientModel):
    _name = 'logistics.awb.print.wizard'
    _description = 'Print shipping labels'

    shipment_ids = fields.Many2many(
        'logistics.shipment',
        'logistics_awb_print_wizard_shipment_rel',
        'wizard_id',
        'shipment_id',
        string='Shipments',
        required=True,
    )
    paper_size = fields.Selection(
        [
            ('a4', 'A4'),
            ('100x150', '100 × 150 mm'),
        ],
        string='Paper size',
        required=True,
        default='a4',
    )

    @api.model
    def _action_open(self, shipments):
        wizard = self.create({
            'shipment_ids': [(6, 0, shipments.ids)],
            'paper_size': 'a4',
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Print shipping labels'),
            'res_model': self._name,
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_print(self):
        self.ensure_one()
        if not self.shipment_ids:
            raise UserError(_('No shipments to print.'))
        xmlid = self.env['logistics.shipment']._awb_report_xmlid_for_paper(
            self.paper_size)
        return self.env.ref(xmlid).report_action(self.shipment_ids)
