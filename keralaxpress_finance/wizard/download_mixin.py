import base64

from odoo import fields, models

XLSX_MIMETYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class KxDownloadMixin(models.AbstractModel):
    """Hand a generated file back to the browser from a wizard button."""
    _name = 'kx.download.mixin'
    _description = 'KeralaXpress report download'

    file_data = fields.Binary(string='File', readonly=True, attachment=False)
    file_name = fields.Char(string='File Name', readonly=True)

    def _kx_download(self, content, filename):
        self.ensure_one()
        self.write({'file_data': base64.b64encode(content), 'file_name': filename})
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/?model=%s&id=%s&field=file_data'
                   '&filename_field=file_name&download=true' % (self._name, self.id),
            'target': 'download',
        }
