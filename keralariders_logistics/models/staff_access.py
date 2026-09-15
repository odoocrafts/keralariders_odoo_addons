from odoo import api, fields, models, _
from odoo.exceptions import AccessError


ADMIN_GROUP_XMLID = 'keralariders_logistics.group_logistics_admin'
STAFF_ADMIN_GROUP_XMLID = 'keralariders_logistics.group_staff_administration'
WALLET_GROUP_XMLID = 'keralariders_logistics.group_staff_wallet'


def kx_can_manage_staff(env):
    """Logistics Administrator or Administration-pack staff may create/edit office users."""
    user = env.user
    return user.has_group(ADMIN_GROUP_XMLID) or user.has_group(STAFF_ADMIN_GROUP_XMLID)


class StaffAccessPack(models.Model):
    _name = 'logistics.staff.access.pack'
    _description = 'Office Access Area'
    _order = 'sequence, id'

    name = fields.Char(string='Area', required=True)
    sequence = fields.Integer(default=10)
    description = fields.Char()
    group_id = fields.Many2one(
        'res.groups', string='Privilege Group', required=True, ondelete='restrict',
    )


class ResUsers(models.Model):
    _inherit = 'res.users'

    kx_is_office_staff = fields.Boolean(
        string='KeralaXpress Office Staff',
        default=False,
        help='Set when this login is created as office staff. Used to list staff on Staff Access.',
    )
    kx_staff_pack_summary = fields.Char(
        string='Access Areas',
        compute='_compute_kx_staff_pack_summary',
    )

    @api.depends('group_ids')
    def _compute_kx_staff_pack_summary(self):
        packs = self.env['logistics.staff.access.pack'].sudo().search([])
        for user in self:
            names = packs.filtered(
                lambda pack: pack.group_id in user.group_ids
            ).mapped('name')
            user.kx_staff_pack_summary = ', '.join(names)

    def action_open_kx_staff_access_wizard(self):
        self.ensure_one()
        if not kx_can_manage_staff(self.env):
            raise AccessError(_(
                "Only a Logistics Administrator or Administration staff can "
                "change office access areas."
            ))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Edit Staff Access'),
            'res_model': 'logistics.create.staff.user.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_user_id': self.id},
        }
