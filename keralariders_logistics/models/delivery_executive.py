from odoo import models, fields, api, _

class DeliveryExecutive(models.Model):
    _name = 'logistics.delivery.executive'
    _description = 'Delivery Executive'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'avatar.mixin']
    _order = 'name'

    name = fields.Char(required=True, tracking=True, string='Executive Name')
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

    is_delivery = fields.Boolean(string='Is Delivery Executive', default=True, help="Indicates if the executive is a delivery executive.")
    is_pickup = fields.Boolean(string='Is Pickup Executive', default=False, help="Indicates if the executive is a pickup executive.")
    is_driver = fields.Boolean(string='Is Hub to Hub Driver', default=False, help="Indicates if the executive is a driver.")
    is_manager = fields.Boolean(string='Is Manager', default=False, help="Indicates if the executive is a manager.")

    def action_grant_portal_access(self):
        self.ensure_one()
        from odoo.exceptions import UserError
        
        if not self.email:
            raise UserError("Delivery Executive must have an email address to grant portal access.")
            
        portal_group = self.env.ref('base.group_portal')
        exec_group = self.env.ref('keralariders_logistics.group_logistics_executive')
        hub_mgr_group = self.env.ref('keralariders_logistics.group_logistics_hub_manager')
        
        if not self.user_id:
            if self.env['res.users'].sudo().search([('login', '=', self.email)]):
                raise UserError("A user with this email already exists.")
                
            user = self.env['res.users'].sudo().create({
                'name': self.name,
                'login': self.email,
                'email': self.email,
                'group_ids': [(4, portal_group.id), (4, exec_group.id)],
            })
            self.user_id = user.id
            user.action_reset_password()
            message = "Portal access granted and invitation email sent!"
        else:
            groups_to_add = []
            if portal_group not in self.user_id.group_ids:
                groups_to_add.append((4, portal_group.id))
            if exec_group not in self.user_id.group_ids:
                groups_to_add.append((4, exec_group.id))
            if groups_to_add:
                self.user_id.sudo().write({'group_ids': groups_to_add})
                message = "Portal access granted!"
            else:
                message = "Executive already has portal access."

        # Hub managers get the hub manager group when flagged
        if self.is_manager and self.user_id and hub_mgr_group not in self.user_id.group_ids:
            self.user_id.sudo().write({'group_ids': [(4, hub_mgr_group.id)]})
                
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

    @api.model_create_multi
    def create(self, vals_list):
        recs = super().create(vals_list)
        # Create Cash and UPI accounts for the executive
        for rec in recs:
            rec.default_cash_account_id = self.env['logistics.account'].create({"name": f'{rec.name} Cash', 'account_type': 'cash'}).id
            rec.default_upi_account_id = self.env['logistics.account'].create({"name": f'{rec.name} UPI', 'account_type': 'bank'}).id
        return recs

    delivered_shipments_count = fields.Integer(compute="_compute_shipments_count")
    total_shipments_count = fields.Integer(compute="_compute_shipments_count")
    pending_shipments_count = fields.Integer(compute="_compute_shipments_count")

    def get_shipments_data(self):
        domain = self._my_tasks_domain()
        all_shipments = self.env['logistics.shipment'].search(domain + [('state', '!=', 'cancelled')])
        # Also include completed shipments for counts (broader than open tasks)
        delivered_extra = self.env['logistics.shipment'].search([
            ('delivery_executive_id', '=', self.id),
            ('state', 'in', ('delivered', 'returned')),
        ])
        all_shipments |= delivered_extra
        delivered_shipments = all_shipments.filtered(
            lambda rec: rec.state in ('delivered', 'returned')
        )
        pending_shipments = (all_shipments - delivered_shipments).filtered(lambda rec: rec.state != 'cancelled')
        return all_shipments, delivered_shipments, pending_shipments

    def _my_tasks_domain(self):
        """Shipments assigned to this DE at shipment or leg level."""
        self.ensure_one()
        return [
            '|', '|', '|', '|',
            ('delivery_executive_id', '=', self.id),
            ('pickup_executive_id', '=', self.id),
            ('custodian_de_id', '=', self.id),
            ('active_leg_id.assigned_de_id', '=', self.id),
            ('estimated_route_ids.assigned_de_id', '=', self.id),
        ]

    def is_eligible_for_operation(self, operation_type):
        """Role check shared with shipment assignment."""
        self.ensure_one()
        return self.env['logistics.shipment']._de_eligible_for_operation(self, operation_type)

    def _compute_shipments_count(self):
        for rec in self:
            all_shipments, delivered_shipments, pending_shipments = rec.get_shipments_data()
            rec.delivered_shipments_count = len(delivered_shipments)
            rec.total_shipments_count = len(all_shipments)
            rec.pending_shipments_count = len(pending_shipments)

    def action_view_delivered_shipments(self):
        self.ensure_one()
        all_shipments, delivered_shipments, pending_shipments = self.get_shipments_data()
        return {
            'name': 'Delivered Shipments',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.shipment',
            'view_mode': 'list,form',
            'domain': [('id', 'in', delivered_shipments.ids)],
            'context': {'default_seller_id': self.id},
        }

    def action_view_pending_shipments(self):
        self.ensure_one()
        all_shipments, delivered_shipments, pending_shipments = self.get_shipments_data()
        return {
            'name': 'Pending Shipments',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.shipment',
            'view_mode': 'list,form',
            'domain': [('id', 'in', pending_shipments.ids)],
            'context': {'default_seller_id': self.id},
        }

    def action_update_password(self):
        return {
            'type': 'ir.actions.act_window',
            'target': 'new',
            'res_model': 'change.password.wizard',
            'view_mode': 'form',
            'context': {
                "active_model": 'res.users',
                'active_ids': self.user_id.ids
            }
        }

    assigned_pickup_pincodes = fields.Many2many('logistics.pincode', 'delivery_executive_pickup_pincode_rel', string="Assigned Pickup Pincodes")
    assigned_delivery_pincodes = fields.Many2many('logistics.pincode', 'delivery_executive_delivery_pincode_rel', string="Assigned Delivery Pincodes")

    @api.model
    def get_assigned_executive_for_pincode(self, pincode, operation_type):
        """Find an active DE covering this pincode for pickup or delivery.

        Role flags: if any role is set, require the matching flag (`is_pickup` /
        `is_delivery`). If no role flags are set, the DE is eligible for both.
        Deterministic: lowest id when multiple match.
        """
        pin = self.env['logistics.pincode'].search([('name', '=', pincode)], limit=1)
        if not pin:
            return self.browse()

        no_role = [
            ('is_pickup', '=', False),
            ('is_delivery', '=', False),
            ('is_driver', '=', False),
            ('is_manager', '=', False),
        ]
        if operation_type == 'pickup':
            pincode_field = 'assigned_pickup_pincodes'
            role_domain = ['|', ('is_pickup', '=', True), '&', '&', '&'] + no_role
        elif operation_type == 'delivery':
            pincode_field = 'assigned_delivery_pincodes'
            role_domain = ['|', ('is_delivery', '=', True), '&', '&', '&'] + no_role
        else:
            raise ValueError("Invalid operation type. Must be either 'pickup' or 'delivery'.")

        domain = [
            ('active', '=', True),
            (pincode_field, 'in', [pin.id]),
        ] + role_domain
        return self.search(domain, order='id', limit=1)