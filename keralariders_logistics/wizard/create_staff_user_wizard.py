from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

from odoo.addons.keralariders_logistics.models.staff_access import (
    kx_can_manage_staff,
)


class CreateStaffUserWizard(models.TransientModel):
    _name = 'logistics.create.staff.user.wizard'
    _description = 'Create or Edit Office Staff User'

    user_id = fields.Many2one('res.users', string='Existing User', readonly=True)
    name = fields.Char(string='Name', required=True)
    login = fields.Char(string='Login / Email', required=True)
    email = fields.Char(string='Email')
    password = fields.Char(string='Password')
    send_invite = fields.Boolean(
        string='Send invitation email',
        default=True,
        help='Uses Odoo’s invite so the staff member sets their own password. '
             'Leave off when you type a password here.',
    )
    pack_ids = fields.Many2many(
        'logistics.staff.access.pack',
        'logistics_create_staff_wizard_pack_rel',
        'wizard_id',
        'pack_id',
        string='Access Areas',
    )
    is_edit = fields.Boolean(compute='_compute_is_edit')

    @api.depends('user_id')
    def _compute_is_edit(self):
        for wizard in self:
            wizard.is_edit = bool(wizard.user_id)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        user_id = res.get('user_id') or self.env.context.get('default_user_id')
        if user_id:
            user = self.env['res.users'].browse(user_id)
            if user.exists():
                packs = self.env['logistics.staff.access.pack'].search([
                    ('group_id', 'in', user.group_ids.ids),
                ])
                res.setdefault('name', user.name)
                res.setdefault('login', user.login)
                res.setdefault('email', user.email or user.login)
                res.setdefault('pack_ids', [(6, 0, packs.ids)])
                res.setdefault('send_invite', False)
        return res

    @api.onchange('login')
    def _onchange_login(self):
        if self.login and not self.email:
            self.email = self.login.strip()

    def _check_manager(self):
        if not kx_can_manage_staff(self.env):
            raise AccessError(_(
                "Only a Logistics Administrator or Administration staff can "
                "create or edit office staff logins."
            ))

    def _normalized_vals(self):
        self.ensure_one()
        name = (self.name or '').strip()
        login = (self.login or '').strip()
        email = (self.email or login).strip()
        if not name:
            raise UserError(_("Name is required."))
        if not login:
            raise UserError(_("Login / email is required."))
        if not self.pack_ids:
            raise UserError(_("Select at least one access area."))
        return name, login, email

    def _group_commands_for_create(self):
        internal = self.env.ref('base.group_user')
        group_ids = [internal.id] + self.pack_ids.mapped('group_id').ids
        return [(6, 0, group_ids)]

    def _apply_pack_groups(self, user):
        """Add/remove only office pack groups. Administration implies Settings/Apps."""
        packs = self.env['logistics.staff.access.pack'].sudo().search([])
        pack_groups = packs.mapped('group_id')
        selected = self.pack_ids.mapped('group_id')
        commands = [(3, group.id) for group in pack_groups]
        commands.extend((4, group.id) for group in selected)
        user.sudo().write({'group_ids': commands})

    def action_apply(self):
        self._check_manager()
        name, login, email = self._normalized_vals()
        Users = self.env['res.users'].sudo()
        portal = self.env.ref('base.group_portal')

        if self.user_id:
            other = Users.search([
                ('login', '=', login),
                ('id', '!=', self.user_id.id),
            ], limit=1)
            if other:
                raise UserError(_("A user with login '%s' already exists.") % login)
            if self.user_id.share:
                raise UserError(_(
                    "Portal users are not office staff. Grant seller or hub "
                    "manager access from those records instead."
                ))
            vals = {
                'name': name,
                'login': login,
                'email': email,
                'kx_is_office_staff': True,
            }
            if self.password:
                vals['password'] = self.password
            self.user_id.sudo().write(vals)
            self._apply_pack_groups(self.user_id)
            user = self.user_id.sudo()
            if portal in user.group_ids:
                user.write({'group_ids': [(3, portal.id)]})
            if self.send_invite and not self.password:
                user.with_context(create_user=1).action_reset_password()
            message = _('Access areas updated for %s.') % name
        else:
            if Users.search([('login', '=', login)], limit=1):
                raise UserError(_("A user with login '%s' already exists.") % login)
            if not self.password and not self.send_invite:
                raise UserError(_(
                    "Set a password or send an invitation email so the staff "
                    "member can sign in."
                ))
            vals = {
                'name': name,
                'login': login,
                'email': email,
                'group_ids': self._group_commands_for_create(),
                'kx_is_office_staff': True,
            }
            if self.password:
                vals['password'] = self.password
            user = Users.create(vals)
            if portal in user.group_ids:
                user.write({'group_ids': [(3, portal.id)]})
            if self.send_invite and not self.password:
                user.with_context(create_user=1).action_reset_password()
            message = _('Staff login created for %s.') % name
            if self.send_invite and not self.password:
                message = _('Staff login created for %s and invitation email sent.') % name

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Office Staff'),
                'message': message,
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
