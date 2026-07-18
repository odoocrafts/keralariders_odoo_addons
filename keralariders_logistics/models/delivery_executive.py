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
