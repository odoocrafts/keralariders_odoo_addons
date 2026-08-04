from odoo import models, fields, api, _
from odoo.exceptions import UserError
import uuid

delivery_states = [
    ('order_added', 'Order Added'),
    ('pickup_requested', 'Pickup Requested'),
    ('picked', 'Picked'),
    ('in_transit', 'In Transit'),
    ('at_source_hub', 'At Source Hub'),
    ('at_central_hub', 'At Central Hub'),
    ('at_destination_hub', 'At Destination Hub'),
    ('out_for_delivery', 'Out for Delivery'),
    ('delivered', 'Delivered'),
    ('cancelled', 'Cancelled'),
    ('return_requested', 'Return Requested'),
    ('return_picked', 'Return Picked'),
    ('returned', 'Returned'),
    ('cancel', 'Cancelled'),
]

class Shipment(models.Model):
    _name = 'logistics.shipment'
    _description = 'Shipment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'name desc, write_date desc'

    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"AWB - {rec.name}"
    
    name = fields.Char(string='Shipment Reference (AWB)', required=True, copy=False, readonly=True, index=True, default=lambda self: _('New'))
    tracking_token = fields.Char(string='Tracking Token', default=lambda self: str(uuid.uuid4()), copy=False, index=True)
    tracking_url = fields.Char(string='Tracking URL', compute='_compute_tracking_url')

    def _compute_tracking_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        for shipment in self:
            if shipment.tracking_token:
                shipment.tracking_url = f"{base_url}/track/{shipment.tracking_token}"
            else:
                shipment.tracking_url = False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].sudo().next_by_code('logistics.shipment') or _('New')
            # Initial custody: package starts with the seller until DE pickup.
            vals.setdefault('custodian_type', 'seller')
        return super(Shipment, self).create(vals_list)

    # -------------------------------------------------------------------------
    # Custody
    # Custody after DE "drop at hub" stays with DE until hub manager receive
    # scan — that receive is the source of truth that sets custodian_type=hub.
    # -------------------------------------------------------------------------
    custodian_type = fields.Selection([
        ('seller', 'Seller'),
        ('de', 'Delivery Executive'),
        ('hub', 'Hub'),
        ('customer', 'Customer'),
    ], string='Custodian', default='seller', tracking=True, index=True)
    custodian_de_id = fields.Many2one(
        'logistics.delivery.executive',
        string='Custodian DE',
        tracking=True,
    )
    current_hub_id = fields.Many2one('logistics.hub', string='Current Hub', tracking=True, index=True)
    active_leg_id = fields.Many2one('logistics.shipment.estimated.route', string='Active Route Leg')
    route_locked = fields.Boolean(
        string='Route Locked',
        default=False,
        help='When set, estimated route is not recomputed (ops have started). Admins can force recompute.',
    )
    event_ids = fields.One2many('logistics.shipment.event', 'shipment_id', string='Custody Events')
    event_count = fields.Integer(compute='_compute_event_count')

    @api.depends('event_ids')
    def _compute_event_count(self):
        for rec in self:
            rec.event_count = len(rec.event_ids)

    def _lock_route(self):
        self.filtered(lambda s: not s.route_locked).write({'route_locked': True})

    def _create_custody_event(self, event_type, to_custodian=None, hub=None, actor_de=None,
                              scanned_code=None, note=None, leg=None):
        """Create a custody event for each shipment in self."""
        Event = self.env['logistics.shipment.event']
        for shipment in self:
            Event.create({
                'shipment_id': shipment.id,
                'event_type': event_type,
                'event_time': fields.Datetime.now(),
                'actor_user_id': self.env.user.id,
                'actor_de_id': actor_de.id if actor_de else False,
                'hub_id': hub.id if hub else (shipment.current_hub_id.id if shipment.current_hub_id else False),
                'leg_id': leg.id if leg else (shipment.active_leg_id.id if shipment.active_leg_id else False),
                'from_custodian_type': shipment.custodian_type,
                'to_custodian_type': to_custodian or shipment.custodian_type,
                'scanned_code': scanned_code,
                'note': note,
            })

    def action_mark_picked(self, actor_de=None, scanned_code=None, note=None):
        """DE confirms pickup from seller. Custody → DE."""
        for shipment in self:
            if shipment.state not in ('pickup_requested', 'order_added'):
                raise UserError(
                    _("Shipment %s cannot be marked picked from state '%s'.")
                    % (shipment.name, shipment.state)
                )
            de = actor_de or shipment.delivery_executive_id or shipment.custodian_de_id
            shipment._create_custody_event(
                'pickup_scan',
                to_custodian='de',
                actor_de=de,
                scanned_code=scanned_code or shipment.name,
                note=note,
            )
            vals = {
                'state': 'picked',
                'picked_on': fields.Datetime.now(),
                'custodian_type': 'de',
                'custodian_de_id': de.id if de else False,
                'current_hub_id': False,
                'estimated_delivery_date': fields.Date.today(),
            }
            if de and not shipment.delivery_executive_id:
                vals['delivery_executive_id'] = de.id
            shipment.write(vals)
            shipment._lock_route()
        return True

    def action_drop_at_hub(self, hub=None, actor_de=None, scanned_code=None, note=None):
        """DE marks package dropped at a hub.

        Custody remains with the DE until hub_receive — hub manager scan is
        the source of truth that transfers custodian_type to hub.
        """
        for shipment in self:
            if shipment.custodian_type != 'de':
                raise UserError(
                    _("Shipment %s must be in DE custody to drop at hub (current: %s).")
                    % (shipment.name, shipment.custodian_type)
                )
            if shipment.state not in ('picked', 'in_transit', 'at_source_hub', 'at_central_hub', 'at_destination_hub'):
                raise UserError(
                    _("Shipment %s cannot be dropped at hub from state '%s'.")
                    % (shipment.name, shipment.state)
                )
            target_hub = hub or shipment.source_hub_id or shipment.current_hub_id
            if not target_hub:
                raise UserError(_("No hub specified for drop of shipment %s.") % shipment.name)
            de = actor_de or shipment.custodian_de_id or shipment.delivery_executive_id
            shipment._create_custody_event(
                'dropped_at_hub',
                to_custodian='de',
                hub=target_hub,
                actor_de=de,
                scanned_code=scanned_code or shipment.name,
                note=note or _("Dropped at hub; awaiting hub receive scan."),
            )
            # Stay in DE custody; optionally move toward in_transit if leaving seller area
            vals = {
                'current_hub_id': target_hub.id,
            }
            if shipment.state == 'picked' and shipment.source_hub_id and target_hub == shipment.source_hub_id:
                # Still picked until hub receives; leave state as picked/in_transit
                vals['state'] = 'in_transit'
            elif shipment.state == 'picked':
                vals['state'] = 'in_transit'
            shipment.write(vals)
            shipment._lock_route()
        return True

    def action_hub_receive(self, hub=None, scanned_code=None, note=None):
        """Hub manager receive scan — source of truth for hub custody."""
        for shipment in self:
            target_hub = hub or shipment.current_hub_id or shipment.source_hub_id
            if not target_hub:
                raise UserError(_("Hub is required to receive shipment %s.") % shipment.name)
            if shipment.state in ('delivered', 'cancelled', 'cancel', 'returned'):
                raise UserError(
                    _("Shipment %s cannot be received at hub in state '%s'.")
                    % (shipment.name, shipment.state)
                )

            # Determine hub-stop state from planned route hubs
            if target_hub == shipment.source_hub_id and target_hub == shipment.destination_hub_id:
                new_state = 'at_source_hub'
            elif target_hub == shipment.source_hub_id:
                new_state = 'at_source_hub'
            elif target_hub == shipment.destination_hub_id:
                new_state = 'at_destination_hub'
            elif target_hub.hub_type == 'main':
                # Optional physical pass-through at Thrissur — log presence without forcing route
                new_state = 'at_central_hub'
            else:
                new_state = 'at_source_hub'

            shipment._create_custody_event(
                'hub_receive',
                to_custodian='hub',
                hub=target_hub,
                scanned_code=scanned_code or shipment.name,
                note=note,
            )
            shipment.write({
                'state': new_state,
                'custodian_type': 'hub',
                'custodian_de_id': False,
                'current_hub_id': target_hub.id,
                'delivery_executive_id': False,  # clear assigned DE until redispatch
            })
            shipment._lock_route()
        return True

    def action_hub_dispatch(self, delivery_executive, hub=None, scanned_code=None, note=None, for_delivery=True):
        """Hub assigns a DE and releases custody (dispatch / out for delivery or transfer)."""
        if not delivery_executive:
            raise UserError(_("A delivery executive is required to dispatch."))
        for shipment in self:
            if shipment.custodian_type != 'hub':
                raise UserError(
                    _("Shipment %s must be in hub custody to dispatch (current: %s).")
                    % (shipment.name, shipment.custodian_type)
                )
            target_hub = hub or shipment.current_hub_id
            if not target_hub:
                raise UserError(_("Current hub is missing on shipment %s.") % shipment.name)

            # Same-district last mile or dest-hub last mile → out_for_delivery;
            # otherwise in_transit for hub-to-hub movement.
            if for_delivery or target_hub == shipment.destination_hub_id:
                new_state = 'out_for_delivery'
                event_type = 'out_for_delivery'
            else:
                new_state = 'in_transit'
                event_type = 'hub_dispatch'

            shipment._create_custody_event(
                event_type,
                to_custodian='de',
                hub=target_hub,
                actor_de=delivery_executive,
                scanned_code=scanned_code or shipment.name,
                note=note,
            )
            shipment.write({
                'state': new_state,
                'custodian_type': 'de',
                'custodian_de_id': delivery_executive.id,
                'delivery_executive_id': delivery_executive.id,
                'current_hub_id': target_hub.id if new_state == 'in_transit' else False,
            })
            shipment._lock_route()
        return True

    def action_central_pass_through(self, hub=None, scanned_code=None, note=None):
        """Optional lightweight Thrissur pass-through event (does not force route)."""
        main_hub = hub or self.env['logistics.hub'].get_main_hub()
        if not main_hub:
            raise UserError(_("No Main Hub (Thrissur) is configured."))
        for shipment in self:
            shipment._create_custody_event(
                'central_pass_through',
                to_custodian=shipment.custodian_type,
                hub=main_hub,
                scanned_code=scanned_code or shipment.name,
                note=note or _("Physical pass-through at main hub recorded."),
            )
            # Optionally reflect physical presence without changing planned route
            if shipment.custodian_type == 'hub':
                shipment.write({
                    'state': 'at_central_hub',
                    'current_hub_id': main_hub.id,
                })
        return True

    def action_mark_delivered(self, actor_de=None, scanned_code=None, note=None, delivery_remarks=None):
        """DE marks delivered — only allowed from out_for_delivery."""
        for shipment in self:
            if shipment.state != 'out_for_delivery':
                raise UserError(
                    _("Shipment %s can only be marked delivered when Out for Delivery "
                      "(current state: %s).")
                    % (shipment.name, shipment.state)
                )
            de = actor_de or shipment.custodian_de_id or shipment.delivery_executive_id
            shipment._create_custody_event(
                'delivered',
                to_custodian='customer',
                actor_de=de,
                scanned_code=scanned_code or shipment.name,
                note=note or delivery_remarks,
            )
            vals = {
                'state': 'delivered',
                'custodian_type': 'customer',
                'custodian_de_id': False,
                'current_hub_id': False,
                'actual_delivery_date': fields.Datetime.now(),
                'delivered_on': fields.Datetime.now(),
            }
            if delivery_remarks is not None:
                vals['delivery_remarks'] = delivery_remarks
            shipment.write(vals)
        return True

    company_id = fields.Many2one('res.company', string='Company', required=True, default=lambda self: self.env.company)

    seller_id = fields.Many2one('logistics.seller', string='Seller', required=True)
    order_id = fields.Many2one('logistics.order', string='Order', ondelete='cascade')
    delivery_executive_id = fields.Many2one('logistics.delivery.executive', string='Delivery Executive')
    order_date = fields.Date(string='Order Date', required=True, default=fields.Date.context_today)

    @api.depends('seller_id')
    def _compute_shippping_from(self):
        for shipment in self:
            if shipment.seller_id:
                shipment.shipping_from_name = shipment.seller_id.name
                shipment.shipping_from_address = '\n'.join([shipment.seller_id.street or '', shipment.seller_id.street2 or '']) if shipment.seller_id.street or shipment.seller_id.street2 else ''
                shipment.shipping_from_zip = shipment.seller_id.zip
                shipment.shipping_from_district_id = shipment.seller_id.district_id
                shipment.shipping_from_state_id = shipment.seller_id.state_id
                shipment.shipping_from_country_id = shipment.seller_id.country_id

    # Shipping From Address
    shipping_from_name = fields.Char(string='Shipping From Name', compute='_compute_shippping_from', store=True, readonly=False)
    shipping_from_address = fields.Text(string='Shipping From Address', compute='_compute_shippping_from', store=True, readonly=False)
    shipping_from_zip = fields.Char(string='Shipping From Pincode', compute='_compute_shippping_from', store=True, readonly=False, required=True)
    @api.onchange('shipping_from_zip')
    def _onchange_shipping_from_zip(self):
        if self.shipping_from_zip:
            pincode_info = self.env['logistics.district'].get_district_from_pincode(self.shipping_from_zip)
            self.shipping_from_district_id = pincode_info['district_id'].id if pincode_info['district_id'] else False
            self.shipping_from_state_id = pincode_info['district_id'].state_id.id if pincode_info['district_id'] else False
    
    shipping_from_district_id = fields.Many2one('logistics.district', string='Shipping From District', compute='_compute_shippping_from', store=True, readonly=False)
    shipping_from_state_id = fields.Many2one('res.country.state', string='Shipping From State', compute='_compute_shippping_from', store=True, readonly=False)
    shipping_from_country_id = fields.Many2one('res.country', string='Shipping From Country', compute='_compute_shippping_from', store=True, readonly=False)

    # Shipping To Address
    shipping_to_name = fields.Char(string='Shipping To Name',)
    shipping_to_address = fields.Text(string='Shipping To Address')
    shipping_to_zip = fields.Char(string='Shipping To Pincode', required=True)
    @api.onchange('shipping_to_zip')
    def _onchange_shipping_to_zip(self):
        if self.shipping_to_zip:
            pincode_info = self.env['logistics.district'].get_district_from_pincode(self.shipping_to_zip)
            self.shipping_to_district_id = pincode_info['district_id'].id if pincode_info['district_id'] else False
            self.shipping_to_state_id = pincode_info['district_id'].state_id.id if pincode_info['district_id'] else False

    shipping_to_district_id = fields.Many2one('logistics.district', string='Shipping To District')
    shipping_to_state_id = fields.Many2one('res.country.state', string='Shipping To State', default=lambda self: self.env.company.state_id.id)
    shipping_to_country_id = fields.Many2one('res.country', string='Shipping To Country', default=lambda self: self.env.company.partner_id.country_id.id) 
    shipping_to_mobile = fields.Char(string='Shipping To Mobile Number')
    shipping_to_email = fields.Char(string='Shipping To Email')

    # Billing Address
    billing_same_as_shipping = fields.Boolean(string='Same as Shipping', default=True)
    billing_name = fields.Char(string='Billing Name',)
    billing_address = fields.Text(string='Billing Address')
    billing_zip = fields.Char(string='Billing Pincode')
    @api.onchange('billing_zip')
    def _onchange_billing_zip(self):
        if self.billing_zip:
            pincode_info = self.env['logistics.district'].get_district_from_pincode(self.billing_zip)
            self.billing_district_id = pincode_info['district_id'].id if pincode_info['district_id'] else False
            self.billing_state_id = pincode_info['district_id'].state_id.id if pincode_info['district_id'] else False

    billing_district_id = fields.Many2one('logistics.district', string='Billing District')
    billing_state_id = fields.Many2one('res.country.state', string='Billing State', default=lambda self: self.env.company.state_id.id)
    billing_country_id = fields.Many2one('res.country', string='Billing Country', default=lambda self: self.env.company.partner_id.country_id.id)

    @api.onchange('shipping_to_name', 'shipping_to_address', 'shipping_to_zip', 'shipping_to_district_id', 'shipping_to_state_id', 'shipping_to_country_id')
    def _onchange_shipping_to_address(self):
        if self.billing_same_as_shipping:
            self.billing_name = self.shipping_to_name
            self.billing_address = self.shipping_to_address
            self.billing_zip = self.shipping_to_zip
            self.billing_district_id = self.shipping_to_district_id
            self.billing_state_id = self.shipping_to_state_id
            self.billing_country_id = self.shipping_to_country_id

    estimated_delivery_date = fields.Date(string='Estimated Delivery Date')
    actual_delivery_date = fields.Datetime(string='Actual Delivery Date')
    delivery_remarks = fields.Text(string="Delivery Remarks")

    order_payment_type = fields.Selection([('prepaid', 'Prepaid'), ('cod', 'COD'), ('na', 'Not Applicable')], string='Order Payment Type', required=True, default='prepaid')
    cod_payment_method = fields.Selection([('cash', 'Cash'), ('upi', 'UPI')], string='COD Payment Method')

    delivery_charges_subtotal = fields.Monetary(string='Delivery Charges (Subtotal)', currency_field='currency_id', compute='_compute_delivery_charges', store=True, readonly=False)
    @api.depends('total_weight', 'shipping_from_district_id', 'shipping_to_district_id', 'tax_percentage')
    def _compute_delivery_charges(self):
        for record in self:
            if record.total_weight:
                same_district = (record.shipping_from_district_id == record.shipping_to_district_id)
                package_id = record.seller_id.delivery_package_id.id if record.seller_id.delivery_package_id else None
                record.delivery_charges_subtotal = self.env['logistics.delivery.charges'].sudo().calculate_delivery_charge(
                    record.total_weight, same_district, package_id=package_id)
            record.delivery_charges_total = record.delivery_charges_subtotal * (1 + record.tax_percentage) if record.tax_percentage else record.delivery_charges_subtotal
    delivery_charges_total = fields.Monetary(string='Delivery Charges (Incl. Tax)', currency_field='currency_id', compute='_compute_delivery_charges', store=True)
    tax_percentage = fields.Float(string='Tax Percentage', default=0)
    total_weight = fields.Float(string='Total Weight (Kg)', digits=(16, 3), default=0.0)
    currency_id = fields.Many2one('res.currency', string='Currency', default=lambda self: self.env.company.currency_id.id)

    item_description = fields.Text(string='Item Description')
    total_order_value = fields.Monetary(string='Total Order Amount', currency_field='currency_id', default=0.0)
    cod_amount = fields.Monetary(string='COD Amount', currency_field='currency_id', default=0.0)
    @api.onchange('order_payment_type')
    def _onchange_order_payment_type(self):
        if self.order_payment_type == 'prepaid':
            if self.cod_payment_transfer_ids:
                raise UserError("Payment method cannot be changed for orders with existing COD Payment Transfers. Please delete all the related Transfers before changing payment type.")
            self.cod_amount = 0.0
        elif self.order_payment_type == 'cod':
            self.cod_amount = self.total_order_value
        else:
            self.cod_amount = 0.0
    seller_notes = fields.Text(string='Seller Notes')

    pickup_requested_on = fields.Datetime(string='Pickup Requested On')
    picked_on = fields.Datetime(string='Picked On')
    delivered_on = fields.Datetime(string='Delivered On')

    state = fields.Selection(delivery_states, string='Delivery Status', default='order_added', tracking=True)

    wallet_transaction_id = fields.Many2one("logistics.wallet.transaction", string="Wallet Transaction (Legacy)")

    def action_add_wallet_transaction(self):
        if not self.seller_id:
            raise UserError(f'Seller must be set before adding Wallet Transaction!')
        if not self.seller_id.wallet_ids:
            raise UserError(f'No Wallets found for this Seller!')
        if not self.wallet_transaction_id:
            wallet = self.seller_id.wallet_ids[0]
            # Check wallet balance
            if wallet.balance < self.delivery_charges_total:
                raise UserError(f'Insufficient balance available in your Wallet. Current balance is {wallet.currency_id.format(wallet.balance)}. Please recharge before proceeding')
            
            self.wallet_transaction_id = self.env['logistics.wallet.transaction'].sudo().create({
                'wallet_id': wallet.id,
                'amount': -self.delivery_charges_total,
                'transaction_date': fields.Date.context_today(self),
                'shipment_id': self.id,
                'reference': self.display_name,
            }).id

    def delete_wallet_transaction(self):
        if not self.wallet_transaction_id:
            raise UserError(f'No transaction linked to this Shipment')
        self.wallet_transaction_id.unlink()

    def action_view_wallet_transaction(self):
        if self.wallet_transaction_id:
            return {
                'name': 'Wallet Transaction',
                'type': 'ir.actions.act_window',
                'res_model': 'logistics.wallet.transaction',
                'view_mode': 'list',
                'domain': [('id', '=', self.wallet_transaction_id.id)],
                'context': {'default_wallet_id': self.wallet_transaction_id.wallet_id.id},
            }

    cod_payment_transfer_ids = fields.Many2many("logistics.account.transfer", string="COD Payment Transfers")
    cod_paid_amount = fields.Monetary(string="COD Paid Amount", compute="_compute_cod_paid_balance_amount", store=True)
    cod_balance_amount = fields.Monetary(string="COD Balance Amount", compute="_compute_cod_paid_balance_amount", store=True)

    @api.depends('cod_payment_transfer_ids', 'cod_payment_transfer_ids.amount', 'cod_amount')
    def _compute_cod_paid_balance_amount(self):
        for rec in self:
            rec.cod_paid_amount = sum(rec.cod_payment_transfer_ids.mapped('amount'))
            rec.cod_balance_amount = rec.cod_amount - rec.cod_paid_amount

    def action_add_cod_payment_transfer(self):
        self.ensure_one()
        from_account = self.env['logistics.account'].search([('account_type', 'in', ('cod_customer'))], limit=1)
        if not from_account:
            raise UserError(f'No COD Customer Account found! Please create atleast on account of type COD Customer Account before proceeding.')
        to_account = self.env['logistics.account'].search([('account_type', 'in', ('bank', 'cash'))], limit=1)
        if not to_account:
            raise UserError(f'No Bank or Cash account found! Please create atleast one Bank or Cash account before proceeding.')
        from_account = from_account[0]
        to_account = to_account[0]
        return {
            'name': 'COD Payment Wizard',
            'type': 'ir.actions.act_window',
            'res_model': 'logistics.cod.payment.wizard',
            'view_mode': 'form',
            'context': {
                'default_shipment_id': self.id,
                'default_amount': self.cod_balance_amount,
                'default_from_account_id': from_account.id,
                'default_to_account_id': to_account.id,
                'default_reference':  f'COD Payment for {self.name}',
                'default_seller_id': self.seller_id.id,

            },
            'target': 'new',
        }

    def action_view_cod_payment_transfers(self):
        self.ensure_one()
        if self.cod_payment_transfer_ids:
            return {
                'name': 'COD Account Transfers',
                'type': 'ir.actions.act_window',
                'res_model': 'logistics.account.transfer',
                'view_mode': 'list,form',
                'domain': [('id', 'in', self.cod_payment_transfer_ids.ids)],
                'context': {"create": 0, "no_create": 1},
            }

    def action_create_payment_cod_from_portal(self, payment_method: str):
        for rec in self:
            if rec.order_payment_type == 'cod' and rec.cod_balance_amount > 0:
                if payment_method == 'upi':
                    from_account_id = self.env['logistics.account'].search([('account_type', '=', 'cod_customer'), ('name', 'ilike', 'upi')], limit=1)
                elif payment_method == 'cash':
                    from_account_id = self.env['logistics.account'].search([('account_type', '=', 'cod_customer'), ('name', 'ilike', 'cash')], limit=1)

                cod_payment_wizard = self.env['logistics.cod.payment.wizard'].create({
                    'from_account_id': from_account_id.id,
                    'to_account_id': from_account_id.id, #dummy to_account
                    'shipment_id': rec.id,
                    'amount': self.cod_balance_amount,
                    'reference':  f'COD Payment for {self.name}',
                    'seller_id': self.seller_id.id,
                })
                cod_payment_wizard.from_account_id = from_account_id.id
                # Compute new to_account from from_account
                cod_payment_wizard._compute_to_account()
                cod_payment_wizard.action_create_transfer()