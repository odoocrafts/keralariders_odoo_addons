from odoo import models, fields, api, _
from odoo.exceptions import UserError


class CreateHubManagerWizard(models.TransientModel):
    _name = 'logistics.create.hub.manager.wizard'
    _description = 'Create Hub Manager Portal User'

    hub_id = fields.Many2one('logistics.hub', string='Hub', required=True)
    name = fields.Char(string='Manager Name', required=True)
    email = fields.Char(string='Email', required=True)
    phone = fields.Char(string='Phone')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if not res.get('hub_id'):
            hub_id = self.env.context.get('default_hub_id') or self.env.context.get('active_id')
            if hub_id and (
                self.env.context.get('active_model') == 'logistics.hub'
                or self.env.context.get('default_hub_id')
            ):
                res['hub_id'] = hub_id
        return res

    def action_create_hub_manager(self):
        self.ensure_one()
        email = (self.email or '').strip()
        name = (self.name or '').strip()
        if not name:
            raise UserError(_("Manager name is required."))
        if not email:
            raise UserError(_("Email is required to create a portal login for the hub manager."))

        Users = self.env['res.users'].sudo()
        if Users.search([('login', '=', email)], limit=1):
            raise UserError(_("A user with login '%s' already exists.") % email)

        portal_group = self.env.ref('base.group_portal')
        hub_mgr_group = self.env.ref('keralariders_logistics.group_logistics_hub_manager')

        partner = self.env['res.partner'].sudo().create({
            'name': name,
            'email': email,
            'phone': self.phone or False,
        })
        # Mirror seller/DE: portal user + invite; also assign hub manager group.
        user = Users.create({
            'name': name,
            'login': email,
            'email': email,
            'partner_id': partner.id,
            'group_ids': [(4, portal_group.id), (4, hub_mgr_group.id)],
        })
        self.hub_id.write({'manager_ids': [(4, user.id)]})
        user.action_reset_password()

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Hub Manager'),
                'message': _('Portal user created for %s and invitation email sent.') % name,
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
