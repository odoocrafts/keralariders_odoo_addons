import os

from odoo import fields, models, _
from odoo.exceptions import UserError

from ..models.indiapost_scan_import import (
    _check_scan_import_user,
    _xlsx_bytes,
)


class IndiapostScanImportWizard(models.TransientModel):
    _name = 'logistics.indiapost.scan.import.wizard'
    _description = 'Upload India Post scan spreadsheet'

    data_file = fields.Binary(string='Spreadsheet', required=True)
    filename = fields.Char(string='Filename')

    def action_import(self):
        """Create one import and post a single adjustment per shipment."""
        self.ensure_one()
        _check_scan_import_user(self.env)
        if not self.data_file:
            raise UserError(_(
                'Upload the India Post seller portal spreadsheet (.xlsx).'
            ))
        raw = _xlsx_bytes(self.data_file)
        filename = os.path.basename(self.filename or 'indiapost_scan.xlsx')
        scan_import = self.env['logistics.indiapost.scan.import'].create_from_xlsx(
            raw, filename)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Scan import'),
            'res_model': 'logistics.indiapost.scan.import',
            'res_id': scan_import.id,
            'view_mode': 'form',
            'target': 'current',
        }
