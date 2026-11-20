from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class Hub(models.Model):
    _name = "logistics.hub"
    _description = "Logistics Hub"
    _order = "name"

    name = fields.Char(string="Hub Name", required=True)
    code = fields.Char(string="Hub Code", index=True)
    district_id = fields.Many2one('logistics.district', string="District", required=True, index=True)
    hub_type = fields.Selection(
        [('district', 'District Hub'), ('main', 'Main Hub')],
        string="Hub Type",
        default='district',
        required=True,
    )
    manager_ids = fields.Many2many(
        'res.users',
        'logistics_hub_manager_rel',
        'hub_id',
        'user_id',
        string="Hub Managers",
    )
    active = fields.Boolean(default=True)
    pincode_ids = fields.Many2many('logistics.pincode', string="Assigned Pincodes")
    pincodes_count = fields.Integer(string="Pincodes Count", compute="_compute_pincodes_count")
    inventory_count = fields.Integer(string="Inventory Count", compute="_compute_inventory_count")
    cash_account_id = fields.Many2one(
        'logistics.account',
        string="Hub Cash Account",
        help="COD cash holdings deposited by delivery executives at this hub.",
    )

    _sql_constraints = [
        ('unique_district_hub', 'unique(district_id)', 'Only one hub is allowed per district.'),
    ]

    def get_or_create_cash_account(self):
        """Return hub cash ledger account, creating it on first use."""
        self.ensure_one()
        if self.cash_account_id:
            return self.cash_account_id
        account = self.env['logistics.account'].search([
            ('hub_id', '=', self.id),
            ('account_type', '=', 'hub'),
        ], limit=1)
        if not account:
            account = self.env['logistics.account'].create({
                'name': f'{self.name} Hub Cash',
                'account_type': 'hub',
                'hub_id': self.id,
                'reference': _('Auto-created hub COD cash account'),
            })
        self.cash_account_id = account.id
        return account

    @api.model_create_multi
    def create(self, vals_list):
        hubs = super().create(vals_list)
        for hub in hubs:
            hub.get_or_create_cash_account()
        return hubs

    @api.constrains('hub_type')
    def _check_one_main_hub(self):
        for rec in self:
            if rec.hub_type == 'main':
                other_main = self.search([
                    ('hub_type', '=', 'main'),
                    ('id', '!=', rec.id),
                ], limit=1)
                if other_main:
                    raise ValidationError(
                        _("Only one Main Hub is allowed. '%s' is already set as Main Hub.")
                        % other_main.name
                    )

    def _compute_pincodes_count(self):
        for rec in self:
            rec.pincodes_count = len(rec.pincode_ids)

    def _compute_inventory_count(self):
        Shipment = self.env['logistics.shipment']
        for rec in self:
            rec.inventory_count = Shipment.search_count([
                ('custodian_type', '=', 'hub'),
                ('current_hub_id', '=', rec.id),
            ])

    def action_view_inventory(self):
        self.ensure_one()
        return {
            'name': _('Hub Inventory — %s') % self.name,
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.shipment',
            'view_mode': 'list,form',
            'domain': [
                ('custodian_type', '=', 'hub'),
                ('current_hub_id', '=', self.id),
            ],
            'context': {'default_current_hub_id': self.id},
        }

    def _district_name_match_keys(self, district_name):
        """Normalize district names for CSV matching (e.g. Kasargod vs KASARAGOD)."""
        name = (district_name or '').strip().lower()
        aliases = {
            'kasargod': {'kasargod', 'kasaragod'},
            'kasaragod': {'kasargod', 'kasaragod'},
        }
        return aliases.get(name, {name})

    def action_assign_district_pincodes(self):
        """Assign all pincodes belonging to this hub's district."""
        Pincode = self.env['logistics.pincode']
        for hub in self:
            if not hub.district_id:
                continue
            keys = self._district_name_match_keys(hub.district_id.name)
            # Prefer district_name from CSV (stored uppercase); fall back to computed district_id
            all_pincodes = Pincode.search([])
            matched = all_pincodes.filtered(
                lambda p, keys=keys, district=hub.district_id: (
                    (p.district_name or '').strip().lower() in keys
                    or (p.district_id and p.district_id.id == district.id)
                )
            )
            hub.pincode_ids = [(6, 0, matched.ids)]
        return True

    def action_open_create_hub_manager_wizard(self):
        """Open wizard to create a new hub manager portal user."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Create Hub Manager'),
            'res_model': 'logistics.create.hub.manager.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_hub_id': self.id,
                'active_id': self.id,
                'active_model': 'logistics.hub',
            },
        }

    def action_grant_managers_portal_access(self):
        """Ensure linked hub managers have portal + hub manager groups.

        Does not create users — use Create Hub Manager for that.
        Validates that each manager has an email suitable for portal login.
        """
        portal_group = self.env.ref('base.group_portal')
        hub_mgr_group = self.env.ref('keralariders_logistics.group_logistics_hub_manager')
        invited = self.env['res.users']
        for hub in self:
            if not hub.manager_ids:
                raise UserError(_(
                    "No hub managers are linked on '%s'. "
                    "Use Create Hub Manager, or link an existing user first."
                ) % hub.name)
            for user in hub.manager_ids:
                email = (user.email or user.login or '').strip()
                if not email or '@' not in email:
                    raise UserError(_(
                        "Hub manager '%s' must have a valid email address "
                        "to grant portal access."
                    ) % user.name)
                groups = []
                newly_portal = portal_group not in user.group_ids
                if newly_portal:
                    groups.append((4, portal_group.id))
                if hub_mgr_group not in user.group_ids:
                    groups.append((4, hub_mgr_group.id))
                if groups:
                    user.sudo().write({'group_ids': groups})
                if newly_portal:
                    invited |= user
        for user in invited:
            user.sudo().action_reset_password()
        message = _('Portal / Hub Manager access granted to linked users.')
        if invited:
            message = _(
                'Portal / Hub Manager access granted. '
                'Invitation email sent to %s newly portal-enabled user(s).'
            ) % len(invited)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Hub Managers'),
                'message': message,
                'type': 'success',
                'sticky': False,
            }
        }

    @api.model
    def get_hub_from_pincode(self, pincode, raise_if_missing=True):
        """Resolve hub for a pincode string.

        Looks up logistics.pincode by name, then finds a hub that includes it.
        Falls back to the hub for the pincode's district. Raises UserError if
        none unless ``raise_if_missing`` is False (India Post national dest).
        """
        if not pincode:
            if raise_if_missing:
                raise UserError(_("Cannot resolve hub: pincode is missing."))
            return self.browse()

        pincode_str = str(pincode).strip()
        pincode_rec = self.env['logistics.pincode'].search([('name', '=', pincode_str)], limit=1)

        if pincode_rec:
            hub = self.search([
                ('pincode_ids', 'in', pincode_rec.ids),
                ('active', '=', True),
            ], limit=1)
            if hub:
                return hub

        district_info = self.env['logistics.district'].get_district_from_pincode(pincode_str)
        district = district_info.get('district_id')
        if district:
            hub = self.search([
                ('district_id', '=', district.id),
                ('active', '=', True),
            ], limit=1)
            if hub:
                return hub

        if not raise_if_missing:
            return self.browse()
        raise UserError(
            _("Cannot find any Hub assigned to pincode '%s' or its district. "
              "Please ensure hubs are seeded and pincodes are assigned.")
            % pincode_str
        )

    @api.model
    def get_main_hub(self):
        return self.search([('hub_type', '=', 'main'), ('active', '=', True)], limit=1)

    @api.model
    def assign_all_hub_pincodes(self):
        """Post-init / migration helper: assign every district's pincodes to its hub."""
        hubs = self.search([])
        hubs.action_assign_district_pincodes()
        return True

    def _kx_awaiting_pickup_domain(self):
        """Shipments these hubs still have to give to a pickup rider.

        Outbound India Post is collected by the post, so it stays out. A
        return is collected from the customer, including when India Post
        carried the outbound leg, and that job is ``return_requested``. The
        customer's hub (destination) and the seller's hub (source) both see it.
        """
        hub_ids = self.exists().ids
        return [
            '|',
            '&', '&',
            ('source_hub_id', 'in', hub_ids),
            ('state', 'in', ('pickup_requested', 'order_added')),
            ('fulfilment_method', '!=', 'indiapost'),
            '&', '&',
            ('is_return_journey', '=', True),
            ('state', '=', 'return_requested'),
            '|',
            ('source_hub_id', 'in', hub_ids),
            ('destination_hub_id', 'in', hub_ids),
        ]

    def _kx_network_rider_rows(self):
        """Own-network riders for these hubs.

        Open rows are parcels already in the rider's custody. Pending rows
        are assigned and not yet in custody. Undeposited COD payments are
        the lines that should add up to the cash balance.
        India Post shipments are left out of both lists.
        """
        hubs = self.exists()
        Shipment = self.env['logistics.shipment'].sudo()
        Executive = self.env['logistics.delivery.executive'].sudo()
        Transfer = self.env['logistics.account.transfer'].sudo()
        executives = Executive.browse()
        pins = hubs.mapped('pincode_ids')
        if pins:
            executives = Executive.search([
                ('active', '=', True),
                '|',
                ('assigned_pickup_pincodes', 'in', pins.ids),
                ('assigned_delivery_pincodes', 'in', pins.ids),
            ])
        hub_touch = [
            '|', '|',
            ('source_hub_id', 'in', hubs.ids),
            ('destination_hub_id', 'in', hubs.ids),
            ('current_hub_id', 'in', hubs.ids),
        ]
        open_states = ('delivered', 'cancelled', 'returned')
        held = Shipment.search([
            ('fulfilment_method', '=', 'own_network'),
            ('state', 'not in', open_states),
            ('custodian_type', '=', 'de'),
            ('custodian_de_id', '!=', False),
        ] + hub_touch)
        assigned = Shipment.search([
            ('fulfilment_method', '=', 'own_network'),
            ('state', 'not in', open_states),
            '|', '|',
            ('pickup_executive_id', '!=', False),
            ('delivery_executive_id', '!=', False),
            ('active_leg_id.assigned_de_id', '!=', False),
        ] + hub_touch)
        executives |= held.mapped('custodian_de_id')
        executives |= assigned.mapped('pickup_executive_id')
        executives |= assigned.mapped('delivery_executive_id')
        executives |= assigned.mapped('active_leg_id.assigned_de_id')
        accounts = executives.mapped('default_cash_account_id')
        cash_lines = Transfer.search([
            ('transfer_type', '=', 'cod_payment'),
            ('to_account_id', 'in', accounts.ids),
            ('hub_deposit_transfer_id', '=', False),
            ('state', '=', 'posted'),
        ]) if accounts else Transfer.browse()
        rows = []
        for executive in executives.sorted(key=lambda rec: (rec.name or '').lower()):
            mine = held.filtered(
                lambda shipment, executive=executive: shipment.custodian_de_id == executive)
            waiting = assigned.filtered(
                lambda shipment, executive=executive: (
                    shipment not in mine and (
                        shipment.pickup_executive_id == executive
                        or shipment.delivery_executive_id == executive
                        or (
                            shipment.active_leg_id
                            and shipment.active_leg_id.assigned_de_id == executive
                        )
                    )
                ))
            account = executive.default_cash_account_id
            undeposited = cash_lines.filtered(
                lambda transfer, account=account: account and transfer.to_account_id == account)
            rows.append({
                'executive': executive,
                'shipments': mine,
                'pending': waiting,
                'cash_lines': undeposited,
                'undeposited_total': sum(undeposited.mapped('amount')),
                'cash_balance': account.balance if account else 0.0,
            })
        return rows
