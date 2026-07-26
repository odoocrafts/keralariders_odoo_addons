from odoo import models, fields, api, _

class DeliveryExecutive(models.Model):
    _name = 'logistics.delivery.executive'
    _description = 'Delivery Executive'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'name'

    name = fields.Char(required=True, tracking=True, string='Executive Name')
    code = fields.Char(readonly=True, copy=False, default='New', string='Executive Code')
    mobile = fields.Char(required=True, string='Mobile Number')
    email = fields.Char(string='Email')
    address = fields.Text(string='Address')
    aadhaar_number = fields.Char(string='Aadhaar Number')
    driving_license = fields.Char(string='Driving License Number')
    vehicle_type = fields.Selection([
        ('bike', 'Bike'),
        ('car', 'Car'),
        ('van', 'Van'),
        ('truck', 'Truck'),
    ], string='Vehicle Type')
    vehicle_number = fields.Char(string='Vehicle Number')
    assigned_region = fields.Char(string='Assigned Region')
    active = fields.Boolean(default=True)
    user_id = fields.Many2one('res.users', string='Related User')

    def action_grant_portal_access(self):
        self.ensure_one()
        from odoo.exceptions import UserError
        
        if not self.email:
            raise UserError("Delivery Executive must have an email address to grant portal access.")
            
        portal_group = self.env.ref('base.group_portal')
        
        if not self.user_id:
            if self.env['res.users'].sudo().search([('login', '=', self.email)]):
                raise UserError("A user with this email already exists.")
                
            user = self.env['res.users'].sudo().create({
                'name': self.name,
                'login': self.email,
                'email': self.email,
                'group_ids': [(4, portal_group.id)]
            })
            self.user_id = user.id
            user.action_reset_password()
            message = "Portal access granted and invitation email sent!"
        else:
            if portal_group not in self.user_id.group_ids:
                self.user_id.sudo().write({'group_ids': [(4, portal_group.id)]})
                message = "Portal access granted!"
            else:
                message = "Executive already has portal access."
                
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Portal Access',
                'message': message,
                'type': 'success',
                'sticky': False,
            }
        }

    default_upi_account_id = fields.Many2one('logistics.account', string="Default UPI Account")
    default_cash_account_id = fields.Many2one('logistics.account', string="Default Cash Account")
