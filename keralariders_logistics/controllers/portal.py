import base64
import logging
import time
import uuid
from datetime import timedelta

from odoo import http, fields, _
from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager
from odoo.http import request
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

# Seller-facing labels for /my/cod_settlements (backend transfer_type keys unchanged).
_COD_SETTLEMENT_SELLER_TYPE_LABELS = {
    'cod_payment': _('COD collected from customer'),
    'cod_clearance': _('Settlement to your account'),
    'cod_withdrawal': _('Payout to your bank'),
    'other': _('Transfer to your bank'),
}


class LogisticsPortal(CustomerPortal):

    @staticmethod
    def _cod_settlement_seller_type_label(transfer_type):
        """Map logistics.account.transfer transfer_type to a seller-friendly portal label."""
        return _COD_SETTLEMENT_SELLER_TYPE_LABELS.get(transfer_type) or transfer_type

    @staticmethod
    def _cod_settlement_seller_type_badge(transfer_type, state=None):
        """Soft badge classes: payments vs payouts (draft stays warning)."""
        if state == 'draft':
            return 'bg-warning text-dark'
        if transfer_type == 'cod_payment':
            return 'bg-info text-dark'
        return 'bg-secondary'

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if request.env.user._is_public():
            return values

        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].sudo().search([('partner_id', '=', partner.id)], limit=1)
        values['is_seller'] = bool(seller)
        
        if seller:
            order_count = request.env['logistics.order'].search_count([('seller_id', '=', seller.id)])
            values['order_count'] = str(order_count) if order_count > 0 else '0 '
            
            shipment_count = request.env['logistics.shipment'].search_count([('seller_id', '=', seller.id)])
            values['shipment_count'] = str(shipment_count) if shipment_count > 0 else '0 '
            
            wallet = request.env['logistics.wallet'].search([('seller_id', '=', seller.id)], limit=1)
            if wallet:
                symbol = wallet.currency_id.symbol or '₹'
                values['wallet_balance'] = f"{symbol} {wallet.balance:,.2f}"
            else:
                values['wallet_balance'] = "0.00"
                
            Transfer = request.env['logistics.account.transfer'].sudo()
            cod_balance_val = Transfer.get_seller_cod_pending_balance(seller)
            symbol = wallet.currency_id.symbol if wallet else '₹'
            values['cod_balance'] = f"{symbol} {cod_balance_val:,.2f}"
                
            values['charge_calculator'] = ' '
            if seller.delivery_package_id:
                values['delivery_package_name'] = seller.delivery_package_id.name
            else:
                default_package = request.env['logistics.delivery.package'].sudo().search([('is_default', '=', True)], limit=1)
                values['delivery_package_name'] = default_package.name if default_package else "Default"

        delivery_executive = request.env['logistics.delivery.executive'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
        values['is_delivery_executive'] = bool(delivery_executive)
        
        if delivery_executive:
            domain = delivery_executive._my_tasks_domain() + [
                ('state', 'not in', ('delivered', 'cancelled')),
            ]
            assigned_shipment_count = request.env['logistics.shipment'].sudo().search_count(domain)
            values['assigned_shipment_count'] = str(assigned_shipment_count) if assigned_shipment_count > 0 else '0 '
            Transfer = request.env['logistics.account.transfer'].sudo()
            undeposited = 0
            if delivery_executive.default_cash_account_id:
                undeposited = Transfer.search_count([
                    ('transfer_type', '=', 'cod_payment'),
                    ('to_account_id', '=', delivery_executive.default_cash_account_id.id),
                    ('hub_deposit_transfer_id', '=', False),
                ])
            values['cod_undeposited_count'] = str(undeposited) if undeposited else '0 '

        managed_hubs = request.env['logistics.hub'].sudo().search([('manager_ids', 'in', request.env.user.ids)])
        values['is_hub_manager'] = bool(managed_hubs)
        if managed_hubs:
            inventory_count = request.env['logistics.shipment'].sudo().search_count([
                ('custodian_type', '=', 'hub'),
                ('current_hub_id', 'in', managed_hubs.ids),
            ])
            values['hub_inventory_count'] = str(inventory_count) if inventory_count > 0 else '0 '
            values['managed_hub_count'] = str(len(managed_hubs))
            Transfer = request.env['logistics.account.transfer'].sudo()
            hub_accounts = managed_hubs.mapped('cash_account_id')
            unbanked = Transfer.search_count([
                ('transfer_type', '=', 'hub_deposit'),
                ('to_account_id', 'in', hub_accounts.ids),
                ('hub_banking_transfer_id', '=', False),
            ]) if hub_accounts else 0
            values['hub_cod_unbanked_count'] = str(unbanked) if unbanked else '0 '
            pending_pickup_domain = self._hub_pending_pickup_domain(managed_hubs)
            unassigned_pickups = request.env['logistics.shipment'].sudo().search_count(
                pending_pickup_domain + [('pickup_executive_id', '=', False)]
            )
            values['hub_pending_pickup_count'] = str(unassigned_pickups) if unassigned_pickups else '0 '
        
        return values
        
    @http.route(['/my/wallet', '/my/wallet/page/<int:page>'], type='http', auth="user", website=True)
    def portal_my_wallet(self, page=1, date_begin=None, date_end=None, sortby=None, **kw):
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        if not seller:
            return request.redirect('/my')
            
        wallet = request.env['logistics.wallet'].search([('seller_id', '=', seller.id)], limit=1)
        if not wallet:
            return request.redirect('/my')
            
        Transaction = request.env['logistics.wallet.transaction']
        domain = [('wallet_id', '=', wallet.id)]
        
        searchbar_sortings = {
            'date': {'label': _('Newest'), 'order': 'transaction_date desc, id desc'},
            'amount': {'label': _('Amount'), 'order': 'amount desc'},
        }
        if not sortby:
            sortby = 'date'
        order = searchbar_sortings[sortby]['order']

        transaction_count = Transaction.search_count(domain)
        pager = portal_pager(
            url="/my/wallet",
            url_args={'date_begin': date_begin, 'date_end': date_end, 'sortby': sortby},
            total=transaction_count,
            page=page,
            step=self._items_per_page
        )
        
        transactions = Transaction.search(domain, order=order, limit=self._items_per_page, offset=pager['offset'])
        
        recharge_requests = request.env['logistics.wallet.recharge.request'].search([('wallet_id', '=', wallet.id)], order='request_date desc')

        values = {
            'wallet': wallet,
            'transactions': transactions,
            'recharge_requests': recharge_requests,
            'page_name': 'wallet',
            'pager': pager,
            'default_url': '/my/wallet',
            'searchbar_sortings': searchbar_sortings,
            'sortby': sortby,
        }
        return request.render("keralariders_logistics.portal_my_wallet", values)

    @http.route(['/my/wallet/recharge'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_wallet_recharge(self, **post):
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        if seller:
            wallet = request.env['logistics.wallet'].search([('seller_id', '=', seller.id)], limit=1)
            amount = float(post.get('amount', 0))
            if amount > 0 and wallet:
                upi_id = request.env['ir.config_parameter'].sudo().get_param('keralariders_logistics.logistics_upi_id')
                if not upi_id:
                    request.session['error'] = "UPI recharge is not configured. Please contact the administrator."
                    return request.redirect('/my/wallet')
                
                # Construct UPI URI
                import urllib.parse
                company_name = urllib.parse.quote_plus(request.env.company.name)
                upi_uri = f"upi://pay?pa={upi_id}&pn={company_name}&am={amount:.2f}&cu=INR"
                encoded_uri = urllib.parse.quote_plus(upi_uri)
                # Odoo's internal barcode generator might be restricted or missing python-qrcode, 
                # so we use a reliable external QR generator for the standard UPI URI.
                qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={encoded_uri}"
                recharge_token = uuid.uuid4().hex
                request.session['wallet_recharge_confirm_token'] = recharge_token

                return request.render("keralariders_logistics.portal_my_wallet_recharge_pay", {
                    'amount': amount,
                    'qr_url': qr_url,
                    'wallet': wallet,
                    'page_name': 'wallet',
                    'recharge_token': recharge_token,
                })
        return request.redirect('/my/wallet')

    @http.route(['/my/wallet/recharge/confirm'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_wallet_recharge_confirm(self, **post):
        posted_token = post.get('recharge_token')
        session_token = request.session.pop('wallet_recharge_confirm_token', None)
        token_ok = bool(posted_token and session_token and posted_token == session_token)

        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        if seller and token_ok:
            wallet = request.env['logistics.wallet'].search([('seller_id', '=', seller.id)], limit=1)
            amount = float(post.get('amount', 0))
            if amount > 0 and wallet:
                cutoff = fields.Datetime.now() - timedelta(seconds=15)
                duplicate = request.env['logistics.wallet.recharge.request'].search([
                    ('seller_id', '=', seller.id),
                    ('wallet_id', '=', wallet.id),
                    ('requested_amount', '=', amount),
                    ('state', '=', 'pending_approval'),
                    ('request_date', '>=', cutoff),
                ], limit=1)
                if duplicate:
                    request.session['success'] = "Your transaction will be manually verified from the backend. Please wait for verification."
                else:
                    try:
                        request.env['logistics.wallet.recharge.request'].create({
                            'seller_id': seller.id,
                            'wallet_id': wallet.id,
                            'requested_amount': amount,
                        })
                        request.session['success'] = "Your transaction will be manually verified from the backend. Please wait for verification."
                    except AccessError:
                        request.session['error'] = "Unable to submit your recharge request. Please contact support."
        elif seller:
            # Duplicate or stale confirm — do not create another recharge request.
            request.session['success'] = "Your transaction will be manually verified from the backend. Please wait for verification."
        return request.redirect('/my/wallet')

    @http.route(['/my/shipments', '/my/shipments/page/<int:page>'], type='http', auth="user", website=True)
    def portal_my_shipments(self, page=1, date_begin=None, date_end=None, sortby=None, **kw):
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        if not seller:
            return request.redirect('/my')
            
        Shipment = request.env['logistics.shipment']
        domain = [('seller_id', '=', seller.id)]
        
        searchbar_sortings = {
            'date': {'label': _('Newest'), 'order': 'create_date desc, id desc'},
            'name': {'label': _('Reference'), 'order': 'name'},
        }
        if not sortby:
            sortby = 'date'
        order = searchbar_sortings[sortby]['order']

        shipment_count = Shipment.search_count(domain)
        pager = portal_pager(
            url="/my/shipments",
            url_args={'date_begin': date_begin, 'date_end': date_end, 'sortby': sortby},
            total=shipment_count,
            page=page,
            step=self._items_per_page
        )
        
        shipments = Shipment.search(domain, order=order, limit=self._items_per_page, offset=pager['offset'])
        
        values = {
            'shipments': shipments,
            'page_name': 'shipment',
            'pager': pager,
            'default_url': '/my/shipments',
            'searchbar_sortings': searchbar_sortings,
            'sortby': sortby,
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_shipments", values)

    def _portal_seller(self):
        """The logged-in user's seller, or an empty recordset."""
        if request.env.user._is_public():
            return request.env['logistics.seller'].browse()
        return request.env['logistics.seller'].search(
            [('partner_id', '=', request.env.user.partner_id.id)], limit=1
        )

    def _portal_shipment_form_values(self, seller, **extra):
        """Shared context for the standalone shipment form and the order forms."""
        districts = request.env['logistics.district'].sudo().search([])
        states = request.env['res.country.state'].sudo().search(
            [('country_id', '=', request.env.company.country_id.id)]
        )
        values = {
            'seller': seller,
            'districts': districts,
            'states': states,
            'hide_indiapost_pickup_date': False,
            'error': request.session.pop('error', None),
            **self._shipment_form_indiapost_values(seller),
        }
        values.update(extra)
        return values

    @http.route(['/my/shipments/new'], type='http', auth="user", website=True)
    def portal_my_shipments_new(self, **kw):
        seller = self._portal_seller()
        if not seller:
            return request.redirect('/my')
        values = self._portal_shipment_form_values(seller, page_name='shipment_new')
        return request.render("keralariders_logistics.portal_my_shipment_new", values)

    @staticmethod
    def _shipment_form_indiapost_values(seller):
        """Extra context the creation form needs when the seller uses India Post."""
        Shipment = request.env['logistics.shipment'].sudo()
        uses_indiapost = seller._ip_uses_indiapost() if seller else False
        earliest = fields.Date.add(
            fields.Date.context_today(Shipment),
            days=request.env['logistics.indiapost.client'].sudo()._ip_settings()[
                'indiapost_pickup_lead_days'],
        )
        article_field = Shipment._fields['indiapost_article_type']
        return {
            'uses_indiapost': uses_indiapost,
            'pickup_slots': Shipment._fields['indiapost_pickup_slot'].selection,
            'indiapost_article_types': [
                {'code': code, 'label': label}
                for code, label in article_field.selection
            ],
            'earliest_pickup_date': earliest,
            # Surfaced in the form so sellers know why the box matters, and
            # mirrored server-side by logistics.shipment._check_indiapost_package.
            'parcel_min_length_cm': 14,
            'parcel_min_breadth_cm': 9,
            'parcel_weight_threshold_g': 500,
            'max_total_dimension_cm': 300,
        }

    def _portal_destination_from_post(self, post, pincode, require_known_pincode=False):
        """Resolve destination district/state from the form, preferring the pincode.

        Bulk upload already looks the district up from the pincode; the web
        form offers a dropdown as a fallback. Unknown pincodes fail the order
        flow the same way a CSV row does, so a charge cannot be computed from
        an empty district pair.
        """
        def _optional_int(key):
            raw = post.get(key)
            if not raw:
                return False
            try:
                return int(raw)
            except (TypeError, ValueError):
                return False

        district_id = _optional_int('shipping_to_district_id')
        state_id = _optional_int('shipping_to_state_id')
        pincode_info = request.env['logistics.district'].sudo().get_district_from_pincode(pincode)
        looked_up = pincode_info.get('district_id') if pincode_info else False
        if looked_up:
            district_id = district_id or looked_up.id
            state_id = state_id or looked_up.state_id.id
        elif require_known_pincode and not district_id:
            raise UserError(_("Unknown pincode %s") % pincode)
        return district_id, state_id

    def _portal_draft_shipment_vals(self, seller, post, order=None,
                                   require_known_pincode=False):
        """Whitelist of shipment create values from a portal POST.

        Charge, tax, wallet and fulfilment fields are never copied from the
        request: the seller's record decides the carrier, and the rate card /
        India Post quote decide the price. A crafted form cannot reopen the
        tampering hole closed in 993b0d1.
        """
        shipping_to_name = (post.get('shipping_to_name') or '').strip()
        shipping_to_mobile = (post.get('shipping_to_mobile') or '').strip()
        shipping_to_address = (post.get('shipping_to_address') or '').strip()
        shipping_to_zip = (post.get('shipping_to_zip') or '').strip()
        item_description = (post.get('item_description') or '').strip()
        missing = []
        if not shipping_to_name:
            missing.append('Customer Name')
        if not shipping_to_mobile:
            missing.append('Phone Number')
        if not shipping_to_address:
            missing.append('Address')
        if not shipping_to_zip:
            missing.append('Pincode')
        if not item_description:
            missing.append('Item Description')
        if missing:
            raise UserError(_("Missing required fields: %s") % ', '.join(missing))

        try:
            total_weight = float(post.get('total_weight') or 0)
        except (TypeError, ValueError):
            raise UserError("Weight must be greater than 0.")
        if total_weight <= 0:
            raise UserError("Weight must be greater than 0.")

        shipping_from_vals = request.env['logistics.shipment'].sudo()._shipping_from_vals_for_seller(seller)
        if not shipping_from_vals.get('shipping_from_zip'):
            raise UserError(_("Update seller pickup pincode before creating shipments."))

        district_id, state_id = self._portal_destination_from_post(
            post, shipping_to_zip, require_known_pincode=require_known_pincode,
        )

        payment_type = (post.get('order_payment_type') or 'prepaid').strip().lower()
        if payment_type not in ('prepaid', 'cod'):
            payment_type = 'prepaid'
        try:
            order_value = float(post.get('total_order_value') or 0)
        except (TypeError, ValueError):
            order_value = 0.0

        package_post = post
        pickup_fallback = post.get('pickup_date') or (
            order.pickup_date if order else None
        )
        if pickup_fallback and not post.get('indiapost_pickup_date'):
            package_post = dict(post, indiapost_pickup_date=pickup_fallback)

        vals = {
            'seller_id': seller.id,
            'shipping_to_name': shipping_to_name,
            'shipping_to_address': shipping_to_address,
            'shipping_to_zip': shipping_to_zip,
            'shipping_to_district_id': district_id,
            'shipping_to_state_id': state_id,
            'shipping_to_mobile': shipping_to_mobile,
            'item_description': item_description,
            'total_weight': total_weight,
            'order_payment_type': payment_type,
            'total_order_value': order_value,
            'billing_same_as_shipping': True,
            'state': 'order_added',
            **shipping_from_vals,
            **self._shipment_package_vals(package_post, seller),
        }
        if order:
            vals['order_id'] = order.id
        return vals

    def _portal_create_draft_shipment(self, seller, post, order=None,
                                      require_known_pincode=False):
        """Create one draft shipment the same way bulk upload does."""
        vals = self._portal_draft_shipment_vals(
            seller, post, order=order,
            require_known_pincode=require_known_pincode,
        )
        shipment = request.env['logistics.shipment'].sudo().create(vals)
        if shipment.order_payment_type == 'cod':
            shipment.cod_amount = shipment.total_order_value
        warning = self._shipment_quote_after_create(shipment)
        return shipment, warning

    @http.route(['/my/shipments/create'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_shipments_create(self, **post):
        seller = self._portal_seller()
        if not seller:
            return request.redirect('/my')

        try:
            shipment, warning = self._portal_create_draft_shipment(seller, post)
            message = f"Shipment '{shipment.name}' saved as Draft!"
            request.session['success'] = f"{message} {warning}".strip()
            return request.redirect('/my/shipments')
        except Exception as e:
            request.session['error'] = str(e)
            return request.redirect('/my/shipments/new')

    @staticmethod
    def _shipment_package_vals(post, seller, prefix=''):
        """Dimension and pickup values from a portal form or CSV row.

        Dimensions are mandatory for India Post sellers: the carrier prices on
        volume and refuses parcels below 14 x 9 cm outright, so a shipment
        without them cannot be booked or even quoted.
        """
        uses_indiapost = seller._ip_uses_indiapost() if seller else False

        def number(key):
            raw = post.get(prefix + key)
            if raw in (None, ''):
                return 0.0
            try:
                return float(raw)
            except (TypeError, ValueError):
                raise UserError(
                    _('"%(value)s" is not a valid measurement for %(field)s.')
                    % {'value': raw, 'field': key.replace('_cm', '')}
                )

        length = number('length_cm')
        breadth = number('breadth_cm')
        height = number('height_cm')
        if uses_indiapost and not (length > 0 and breadth > 0 and height > 0):
            raise UserError(_(
                'Length, breadth and height (in cm) are required. India Post '
                'prices on package size as well as weight.'
            ))

        vals = {
            'length_cm': length,
            'breadth_cm': breadth,
            'height_cm': height,
            'is_cylindrical': bool(post.get(prefix + 'is_cylindrical')),
        }
        if uses_indiapost:
            vals['indiapost_article_type'] = LogisticsPortal._ip_article_type_from_post(
                post, prefix=prefix)
            slot = post.get(prefix + 'indiapost_pickup_slot')
            valid_slots = dict(
                request.env['logistics.shipment']
                ._fields['indiapost_pickup_slot'].selection)
            vals['indiapost_pickup_slot'] = slot if slot in valid_slots \
                else next(iter(valid_slots))
            pickup_date = post.get(prefix + 'indiapost_pickup_date')
            if pickup_date:
                vals['indiapost_pickup_date'] = pickup_date
        return vals

    # Above this many shipments, pricing the upload inline would mean one API
    # round trip per row inside a web request. Those orders are priced when the
    # seller requests pickup instead.
    _BULK_INLINE_QUOTE_LIMIT = 25

    def _bulk_quote_indiapost(self, order):
        """Price the India Post shipments of a freshly uploaded order."""
        shipments = order.shipment_ids.filtered(
            lambda s: s.is_indiapost and s.indiapost_needs_quote)
        if not shipments:
            return ''
        if len(shipments) > self._BULK_INLINE_QUOTE_LIMIT:
            return _(
                'Delivery charges will be confirmed from live India Post rates '
                'when you request pickup.'
            )
        failures = 0
        for shipment in shipments:
            try:
                with request.env.cr.savepoint():
                    shipment.sudo()._ip_quote_and_store()
            except Exception:
                failures += 1
                _logger.warning('India Post rate lookup failed for %s',
                                shipment.name, exc_info=True)
        if failures:
            return _(
                '%s shipment(s) could not be priced against India Post just '
                'now; their charges will be confirmed when you request pickup.'
            ) % failures
        return ''

    @staticmethod
    def _shipment_quote_after_create(shipment):
        """Price an India Post shipment right after creation.

        Returns a short message for the seller. A rate lookup failure must not
        undo a valid shipment, so the record is kept and simply flagged as
        needing a quote; the wallet debit at pickup time re-quotes anyway.
        """
        if not shipment.is_indiapost:
            return ''
        try:
            shipment.sudo()._ip_quote_and_store()
        except Exception:
            _logger.warning(
                'India Post rate lookup failed for new shipment %s',
                shipment.name, exc_info=True,
            )
            return _(
                'India Post rates could not be fetched just now, so the '
                'delivery charge will be confirmed when you request pickup.'
            )
        return _('India Post charge: %s.') % shipment.currency_id.format(
            shipment.delivery_charges_total)
            
    @http.route(['/my/shipments/bulk_upload/template'], type='http', auth="user", website=True)
    def portal_my_shipments_bulk_upload_template(self, **kw):
        import csv
        import io

        seller = request.env['logistics.seller'].sudo().search(
            [('partner_id', '=', request.env.user.partner_id.id)], limit=1)
        uses_indiapost = seller._ip_uses_indiapost() if seller else True

        output = io.StringIO()
        writer = csv.writer(output)
        # Columns marked * / (mandatory) must be filled; upload accepts both marked and plain headers.
        headers = [
            'Customer Name*',
            'Phone Number* (mandatory)',
            'Address* (mandatory)',
            'Pincode* (mandatory)',
            'Weight (kg)*',
            'Item Description*',
            'Payment Type (prepaid/cod)',
            'Total Order Value',
        ]
        # Dimensions are only mandatory for India Post sellers, but they are
        # offered to everyone so a seller moving between carriers does not have
        # to change their spreadsheet.
        dimension_headers = [
            'Length (cm)*' if uses_indiapost else 'Length (cm)',
            'Breadth (cm)*' if uses_indiapost else 'Breadth (cm)',
            'Height (cm)*' if uses_indiapost else 'Height (cm)',
        ]
        writer.writerow(headers + dimension_headers)

        # Add sample rows to help the user
        writer.writerow(['John Doe', '9876543210', '123 Main St, Apt 4B', '682001',
                         '1.5', 'Electronics', 'prepaid', '0', '30', '20', '15'])
        writer.writerow(['Jane Smith', '9988776655', '456 Market Road', '695001',
                         '2.0', 'Clothing', 'cod', '1500', '25', '18', '10'])
        if uses_indiapost:
            writer.writerow([])
            writer.writerow([
                'India Post prices on size as well as weight. Parcels over 500 g '
                'must measure at least 14 cm x 9 cm, and length + breadth + '
                'height must not exceed 300 cm. Pad small heavy items out to at '
                'least 14 x 9 x 1 cm or they cannot be shipped at all.'
            ])

        csv_content = output.getvalue()

        headers = [
            ('Content-Type', 'text/csv'),
            ('Content-Disposition', 'attachment; filename="Shipments_Bulk_Upload_Template.csv"'),
        ]
        return request.make_response(csv_content, headers=headers)

    @http.route(['/my/orders', '/my/orders/page/<int:page>'], type='http', auth="user", website=True)
    def portal_my_orders(self, page=1, date_begin=None, date_end=None, sortby=None, **kw):
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        if not seller:
            return request.redirect('/my')
            
        Order = request.env['logistics.order']
        domain = [('seller_id', '=', seller.id)]
        
        searchbar_sortings = {
            'date': {'label': _('Newest'), 'order': 'create_date desc, id desc'},
            'name': {'label': _('Reference'), 'order': 'name'},
        }
        if not sortby:
            sortby = 'date'
        order = searchbar_sortings[sortby]['order']

        order_count = Order.search_count(domain)
        pager = portal_pager(
            url="/my/orders",
            url_args={'date_begin': date_begin, 'date_end': date_end, 'sortby': sortby},
            total=order_count,
            page=page,
            step=self._items_per_page
        )
        
        orders = Order.search(domain, order=order, limit=self._items_per_page, offset=pager['offset'])
        
        values = {
            'orders': orders,
            'page_name': 'order',
            'pager': pager,
            'default_url': '/my/orders',
            'searchbar_sortings': searchbar_sortings,
            'sortby': sortby,
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_orders", values)

    @http.route(['/my/orders/<int:order_id>'], type='http', auth="user", website=True)
    def portal_my_order_detail(self, order_id=None, **kw):
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        if not seller:
            return request.redirect('/my')
            
        order = request.env['logistics.order'].search([('id', '=', order_id), ('seller_id', '=', seller.id)], limit=1)
        if not order:
            return request.redirect('/my/orders')
            
        wallet = request.env['logistics.wallet'].search([('seller_id', '=', seller.id)], limit=1)
        
        values = {
            'order': order,
            'wallet': wallet,
            'page_name': 'order',
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_order_detail", values)

    @http.route(['/my/orders/<int:order_id>/print'], type='http', auth="user", website=True)
    def portal_my_order_print(self, order_id=None, **kw):
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        if not seller:
            return request.redirect('/my')
            
        order = request.env['logistics.order'].search([('id', '=', order_id), ('seller_id', '=', seller.id)], limit=1)
        if not order:
            return request.redirect('/my/orders')
            
        shipment_ids = order.shipment_ids.ids
        if not shipment_ids:
            request.session['error'] = "No shipments found for this order."
            return request.redirect(f'/my/orders/{order.id}')
            
        # Create a comma-separated string of shipment IDs
        shipment_ids_str = ",".join(str(s_id) for s_id in shipment_ids)
        return request.redirect(f'/report/pdf/keralariders_logistics.action_report_shipment/{shipment_ids_str}')

    @http.route(['/my/orders/new'], type='http', auth="user", website=True)
    def portal_my_orders_new(self, **kw):
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        if not seller:
            return request.redirect('/my')
            
        values = {
            'page_name': 'order_new',
            'seller': seller,
            'error': request.session.pop('error', None),
        }
        return request.render("keralariders_logistics.portal_my_order_new", values)

    def _portal_seller_order(self, seller, order_id):
        """The seller's order, or an empty recordset if it is not theirs."""
        if not seller or not order_id:
            return request.env['logistics.order'].browse()
        return request.env['logistics.order'].search([
            ('id', '=', order_id),
            ('seller_id', '=', seller.id),
        ], limit=1)

    @http.route(['/my/orders/manual'], type='http', auth="user", website=True)
    def portal_my_orders_manual(self, **kw):
        seller = self._portal_seller()
        if not seller:
            return request.redirect('/my')
        values = self._portal_shipment_form_values(
            seller, page_name='order_manual', hide_indiapost_pickup_date=True,
        )
        return request.render("keralariders_logistics.portal_my_order_manual", values)

    @http.route(['/my/orders/create'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_orders_create(self, **post):
        """Create one order and its first shipment from the portal form.

        Validates the shipment before inserting the order so a missing
        customer / pincode / weight cannot leave a headless draft behind.
        Charge and fulfilment fields in the POST are ignored; the same
        create path as bulk upload prices the parcel.
        """
        seller = self._portal_seller()
        if not seller:
            return request.redirect('/my')
        try:
            pickup_date = (post.get('pickup_date') or '').strip()
            if not pickup_date:
                raise UserError(_("Pickup date is required."))
            vals = self._portal_draft_shipment_vals(
                seller, post, require_known_pincode=True,
            )
            with request.env.cr.savepoint():
                order = request.env['logistics.order'].sudo().create({
                    'seller_id': seller.id,
                    'pickup_date': pickup_date,
                })
                vals['order_id'] = order.id
                shipment = request.env['logistics.shipment'].sudo().create(vals)
                if shipment.order_payment_type == 'cod':
                    shipment.cod_amount = shipment.total_order_value
                warning = self._shipment_quote_after_create(shipment)
            message = _("Order '%s' created with 1 shipment.") % order.name
            request.session['success'] = f"{message} {warning}".strip()
            return request.redirect(f'/my/orders/{order.id}')
        except Exception as e:
            request.session['error'] = str(e)
            return request.redirect('/my/orders/manual')

    @http.route(
        ['/my/orders/<int:order_id>/shipments/new'],
        type='http', auth="user", website=True,
    )
    def portal_my_order_shipment_new(self, order_id=None, **kw):
        seller = self._portal_seller()
        if not seller:
            return request.redirect('/my')
        order = self._portal_seller_order(seller, order_id)
        if not order:
            return request.redirect('/my/orders')
        if order.state != 'draft':
            request.session['error'] = _(
                "Shipments can only be added while the order is still a draft."
            )
            return request.redirect(f'/my/orders/{order.id}')
        values = self._portal_shipment_form_values(
            seller,
            page_name='order_shipment_new',
            order=order,
            hide_indiapost_pickup_date=True,
        )
        return request.render(
            "keralariders_logistics.portal_my_order_shipment_new", values,
        )

    @http.route(
        ['/my/orders/<int:order_id>/shipments/create'],
        type='http', auth="user", website=True, methods=['POST'],
    )
    def portal_my_order_shipment_create(self, order_id=None, **post):
        seller = self._portal_seller()
        if not seller:
            return request.redirect('/my')
        order = self._portal_seller_order(seller, order_id)
        if not order:
            return request.redirect('/my/orders')
        if order.state != 'draft':
            request.session['error'] = _(
                "Shipments can only be added while the order is still a draft."
            )
            return request.redirect(f'/my/orders/{order.id}')
        try:
            _shipment, warning = self._portal_create_draft_shipment(
                seller, post, order=order, require_known_pincode=True,
            )
            message = _("Shipment added to order '%s'.") % order.name
            request.session['success'] = f"{message} {warning}".strip()
            return request.redirect(f'/my/orders/{order.id}')
        except Exception as e:
            request.session['error'] = str(e)
            return request.redirect(f'/my/orders/{order.id}/shipments/new')

    @http.route(['/my/orders/bulk_upload'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_orders_bulk_upload(self, **post):
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        if not seller:
            return request.redirect('/my')
            
        csv_file = post.get('csv_file')
        pickup_date = post.get('pickup_date')
        if not csv_file or not pickup_date:
            request.session['error'] = "Missing file or pickup date."
            return request.redirect('/my/orders/new')

        # Seller origin (from) must exist before any shipment INSERT — required column.
        Shipment = request.env['logistics.shipment'].sudo()
        shipping_from_vals = Shipment._shipping_from_vals_for_seller(seller)
        if not shipping_from_vals.get('shipping_from_zip'):
            request.session['error'] = "Update seller pickup pincode before uploading"
            return request.redirect('/my/orders/new')

        order = None
        try:
            import csv
            import io
            import re
            
            def _csv_cell(row, *aliases):
                """Read a cell by exact or normalized header (strips * / (mandatory))."""
                for alias in aliases:
                    val = row.get(alias)
                    if val is not None and str(val).strip() != '':
                        return str(val).strip()
                normalized = {
                    re.sub(r'\s*\(mandatory\)\s*', '', re.sub(r'\*+', '', (k or ''))).strip().lower(): v
                    for k, v in row.items()
                }
                for alias in aliases:
                    key = re.sub(r'\s*\(mandatory\)\s*', '', re.sub(r'\*+', '', alias)).strip().lower()
                    val = normalized.get(key)
                    if val is not None and str(val).strip() != '':
                        return str(val).strip()
                return ''
            
            file_content = csv_file.read().decode('utf-8')
            csv_reader = csv.DictReader(io.StringIO(file_content))
            
            success_count = 0
            failed_count = 0
            failure_reasons = []
            
            order = request.env['logistics.order'].sudo().create({
                'seller_id': seller.id,
                'pickup_date': pickup_date,
            })
            
            for row_num, row in enumerate(csv_reader, start=2):
                customer_name = _csv_cell(row, 'Customer Name*', 'Customer Name')
                phone = _csv_cell(row, 'Phone Number* (mandatory)', 'Phone Number*', 'Phone Number')
                address = _csv_cell(row, 'Address* (mandatory)', 'Address*', 'Address')
                pincode = _csv_cell(row, 'Pincode* (mandatory)', 'Pincode*', 'Pincode')
                weight_str = _csv_cell(row, 'Weight (kg)*', 'Weight (kg)')
                description = _csv_cell(row, 'Item Description*', 'Item Description')
                payment_type = (_csv_cell(row, 'Payment Type (prepaid/cod)') or '').strip().lower()
                order_value_str = _csv_cell(row, 'Total Order Value') or '0'

                missing_fields = []
                if not customer_name:
                    missing_fields.append('Customer Name')
                if not phone:
                    missing_fields.append('Phone Number')
                if not address:
                    missing_fields.append('Address')
                if not pincode:
                    missing_fields.append('Pincode')
                if not weight_str:
                    missing_fields.append('Weight')
                if not description:
                    missing_fields.append('Item Description')
                if missing_fields:
                    failed_count += 1
                    if len(failure_reasons) < 5:
                        failure_reasons.append(
                            f"Row {row_num}: missing {', '.join(missing_fields)}"
                        )
                    continue
                    
                try:
                    weight = float(weight_str)
                    order_value = float(order_value_str) if order_value_str else 0.0
                except ValueError:
                    failed_count += 1
                    if len(failure_reasons) < 5:
                        failure_reasons.append(f"Row {row_num}: invalid weight or order value")
                    continue
                    
                district_id = False
                state_id = False
                pincode_info = request.env['logistics.district'].sudo().get_district_from_pincode(pincode)
                if pincode_info and pincode_info.get('district_id'):
                    district_id = pincode_info['district_id'].id
                    state_id = pincode_info['district_id'].state_id.id
                else:
                    failed_count += 1
                    if len(failure_reasons) < 5:
                        failure_reasons.append(f"Row {row_num}: unknown pincode {pincode}")
                    continue
                    
                if payment_type not in ['prepaid', 'cod']:
                    payment_type = 'prepaid'

                try:
                    package_vals = self._shipment_package_vals({
                        'length_cm': _csv_cell(row, 'Length (cm)*', 'Length (cm)'),
                        'breadth_cm': _csv_cell(row, 'Breadth (cm)*', 'Breadth (cm)'),
                        'height_cm': _csv_cell(row, 'Height (cm)*', 'Height (cm)'),
                        'indiapost_pickup_date': pickup_date,
                    }, seller)
                except UserError as exc:
                    failed_count += 1
                    if len(failure_reasons) < 5:
                        failure_reasons.append(
                            f"Row {row_num}: {exc.args[0] if exc.args else exc}")
                    continue

                shipment_vals = {
                    'order_id': order.id,
                    'seller_id': seller.id,
                    'shipping_to_name': customer_name,
                    'shipping_to_address': address,
                    'shipping_to_zip': pincode,
                    'shipping_to_district_id': district_id,
                    'shipping_to_state_id': state_id,
                    'shipping_to_mobile': phone,
                    'item_description': description,
                    'total_weight': weight,
                    'order_payment_type': payment_type,
                    'total_order_value': order_value,
                    'billing_same_as_shipping': True,
                    'state': 'order_added',
                    # Seller origin (pickup) — mirrors single-shipment / compute from seller
                    **shipping_from_vals,
                    **package_vals,
                }

                try:
                    # A savepoint so one unshippable row cannot poison the
                    # transaction for the rows that follow it.
                    with request.env.cr.savepoint():
                        shipment = Shipment.create(shipment_vals)
                except (UserError, ValidationError) as exc:
                    # Most often a package India Post cannot carry. Report the
                    # row rather than failing the whole upload.
                    failed_count += 1
                    if len(failure_reasons) < 5:
                        failure_reasons.append(
                            f"Row {row_num}: {exc.args[0] if exc.args else exc}")
                    continue
                if shipment.order_payment_type == 'cod':
                    shipment.cod_amount = shipment.total_order_value
                success_count += 1

            quote_note = self._bulk_quote_indiapost(order)

            if success_count == 0:
                order.sudo().unlink()
                detail = ('; '.join(failure_reasons)) if failure_reasons else ''
                request.session['error'] = (
                    "All rows failed validation (phone, address, and pincode are mandatory). "
                    "Order not created."
                    + (f" {detail}" if detail else "")
                )
                return request.redirect('/my/orders/new')
                
            msg = f"Order created with {success_count} shipments."
            if failed_count > 0:
                msg += f" {failed_count} rows failed validation and were skipped."
                if failure_reasons:
                    msg += " " + '; '.join(failure_reasons)
            if quote_note:
                msg += " " + quote_note

            request.session['success'] = msg
            return request.redirect(f'/my/orders/{order.id}')

        except UserError as e:
            if order and not order.shipment_ids:
                order.sudo().unlink()
            request.session['error'] = e.args[0] if e.args else str(e)
            return request.redirect('/my/orders/new')
        except UnicodeDecodeError:
            if order and not order.shipment_ids:
                order.sudo().unlink()
            request.session['error'] = "Error reading file. Please ensure it is a valid CSV file saved with UTF-8 encoding."
            return request.redirect('/my/orders/new')
        except Exception as e:
            if order and not order.shipment_ids:
                order.sudo().unlink()
            err = str(e)
            # Avoid exposing raw Postgres constraint / SQL errors on the portal.
            if 'shipping_from_zip' in err or 'not-null' in err.lower() or 'NotNullViolation' in type(e).__name__:
                request.session['error'] = "Update seller pickup pincode before uploading"
            else:
                request.session['error'] = "Error processing file. Please check your CSV and try again."
            return request.redirect('/my/orders/new')
            
    @http.route(['/my/orders/request_pickup'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_orders_request_pickup(self, **post):
        order_id = int(post.get('order_id', 0))
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        
        order = request.env['logistics.order'].search([
            ('id', '=', order_id), 
            ('seller_id', '=', seller.id),
            ('state', '=', 'draft')
        ], limit=1)
        
        if not order:
            request.session['error'] = "Order not found or not in Draft state."
            return request.redirect('/my/orders')
            
        try:
            order.sudo().action_request_pickup()
            request.session['success'] = (
                f"Pickup requested successfully for Order {order.name}. "
                f"{order.total_charges} deducted from wallet."
            )
            ip_failed = order.shipment_ids.filtered(
                lambda s: s.fulfilment_method == 'indiapost'
                and s.indiapost_booking_state == 'error')
            if ip_failed:
                details = '\n'.join(
                    '%s: %s' % (
                        shipment.name,
                        shipment.indiapost_booking_error
                        or _('India Post booking failed.'),
                    )
                    for shipment in ip_failed
                )
                request.session['error'] = _(
                    'Pickup was requested and your wallet was charged, but '
                    'India Post could not book the shipment:\n%s'
                ) % details
        except Exception as e:
            request.session['error'] = str(e)
            
        return request.redirect(f'/my/orders/{order.id}')

    @http.route(['/my/shipments/<int:shipment_id>/indiapost_label'], type='http',
                auth="user", website=True)
    def portal_my_shipment_indiapost_label(self, shipment_id=None, **kw):
        """Serve the stored India Post label PDF to the owning seller.

        Fetched on demand the first time, because a label is only useful once
        the article has been booked and most sellers never open it at all.
        """
        seller = request.env['logistics.seller'].sudo().search(
            [('partner_id', '=', request.env.user.partner_id.id)], limit=1)
        if not seller:
            return request.redirect('/my')
        shipment = request.env['logistics.shipment'].sudo().search([
            ('id', '=', shipment_id),
            ('seller_id', '=', seller.id),
            ('indiapost_article_number', '!=', False),
        ], limit=1)
        if not shipment:
            request.session['error'] = _(
                'That shipment has not been booked with India Post yet, so it '
                'has no address label.'
            )
            return request.redirect('/my/shipments')

        if not shipment.indiapost_label_pdf:
            try:
                shipment.action_indiapost_fetch_label()
            except Exception:
                _logger.warning('India Post label fetch failed for %s',
                                shipment.name, exc_info=True)
        if not shipment.indiapost_label_pdf:
            request.session['error'] = _(
                'The India Post label for %s could not be downloaded. Please '
                'try again shortly.'
            ) % shipment.name
            return request.redirect('/my/shipments')

        pdf = base64.b64decode(shipment.indiapost_label_pdf)
        filename = shipment.indiapost_label_filename or (
            '%s.pdf' % shipment.indiapost_article_number)
        return request.make_response(pdf, headers=[
            ('Content-Type', 'application/pdf'),
            ('Content-Length', len(pdf)),
            ('Content-Disposition', f'inline; filename="{filename}"'),
        ])

    @http.route(['/my/shipments/request_return'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_shipments_request_return(self, **post):
        shipment_id = int(post.get('shipment_id', 0))
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        
        shipment = request.env['logistics.shipment'].search([
            ('id', '=', shipment_id),
            ('seller_id', '=', seller.id),
            '|',
            ('state', '=', 'delivered'),
            '&', '&', '&',
            ('state', '=', 'delivery_failed'),
            ('custodian_type', '=', 'hub'),
            ('delivery_fail_count', '>=', 1),
            ('is_return_journey', '=', False),
        ], limit=1)
        
        if not shipment:
            request.session['error'] = "Shipment not found or not eligible for return."
            return request.redirect('/my/shipments')
            
        try:
            # Free reverse journey: customer pickup → hubs → seller (no wallet debit)
            # From delivery_failed at hub, customer pickup is skipped.
            shipment.sudo().action_request_return()
            request.session['success'] = (
                f"Free return requested for {shipment.name}. "
                f"No wallet deduction."
            )
        except UserError as e:
            request.session['error'] = str(e)
        except Exception as e:
            request.session['error'] = str(e)
            
        return request.redirect('/my/shipments')

    # -------------------------------------------------------------------------
    # Rate calculator
    #
    # This route is auth="public", so an anonymous visitor reaches it with no
    # seller context. Anonymous visitors get India Post rates, which is what
    # KeralaXpress sells today; a logged-in seller gets whatever their own
    # fulfilment method is, so sellers still on the hub network keep seeing the
    # weight slab price.
    # -------------------------------------------------------------------------
    _CALCULATOR_MAX_QUOTES = 25
    _CALCULATOR_WINDOW_SECONDS = 300

    @staticmethod
    def _format_calculator_weight(weight):
        """Human-readable weight for the result card, e.g. '1.5 kg'."""
        try:
            value = float(weight)
        except (TypeError, ValueError):
            return False
        formatted = f"{value:.3f}".rstrip('0').rstrip('.')
        return f"{formatted} kg"

    @staticmethod
    def _calculator_seller():
        """The logged-in visitor's seller record, if any."""
        if request.env.user._is_public():
            return request.env['logistics.seller'].sudo().browse()
        # user_id on logistics.seller is computed and not stored, so search by
        # partner_id.
        return request.env['logistics.seller'].sudo().search(
            [('partner_id', '=', request.env.user.partner_id.id)], limit=1
        )

    def _calculator_method(self, seller):
        """Which pricing model applies to this visitor.

        Falls back to the slab table whenever the India Post integration is
        switched off, so the calculator is never dead.
        """
        Client = request.env['logistics.indiapost.client'].sudo()
        if not Client._ip_is_configured():
            return 'own_network'
        if seller:
            return seller._ip_fulfilment_method()
        return 'indiapost'

    def _calculator_throttled(self):
        """Crude per-session cap so the public page cannot be used as a proxy.

        The quote cache absorbs repeated identical requests; this covers the
        case of someone walking a range of weights and pincodes.
        """
        now = time.time()
        stamps = [
            stamp for stamp in (request.session.get('ip_calc_stamps') or [])
            if now - stamp < self._CALCULATOR_WINDOW_SECONDS
        ]
        throttled = len(stamps) >= self._CALCULATOR_MAX_QUOTES
        if not throttled:
            stamps.append(now)
        request.session['ip_calc_stamps'] = stamps
        return throttled

    @staticmethod
    def _ip_article_type_from_post(post, prefix=''):
        """Only Speed Post (SP) and Business Parcel (BP) are bookable products."""
        selection = dict(
            request.env['logistics.shipment']
            ._fields['indiapost_article_type'].selection
        )
        raw = (post.get(prefix + 'indiapost_article_type') or 'SP').strip().upper()
        return raw if raw in selection else 'SP'

    def _calculator_values(self, form=None, quote=None, error=None):
        seller = self._calculator_seller()
        method = self._calculator_method(seller)
        form = dict(form or {})
        if method == 'indiapost' and seller and not form.get('origin_pincode'):
            form['origin_pincode'] = (seller.zip or '').strip()
        if method == 'indiapost' and not form.get('indiapost_article_type'):
            form['indiapost_article_type'] = 'SP'
        article_field = request.env['logistics.shipment']._fields[
            'indiapost_article_type']
        return {
            'page_name': 'calculator',
            'districts': request.env['logistics.district'].sudo().search([]),
            'method': method,
            'is_indiapost': method == 'indiapost',
            'seller': seller,
            'form': form,
            'quote': quote,
            'error': error,
            'indiapost_article_types': [
                {'code': code, 'label': label}
                for code, label in article_field.selection
            ],
            'pickup_slots': [
                {'code': code, 'label': label}
                for code, label in request.env['logistics.shipment']
                ._fields['indiapost_pickup_slot'].selection
            ],
        }

    @http.route(['/my/calculator'], type='http', auth="public", website=True)
    def portal_my_calculator(self, **kw):
        return request.render(
            "keralariders_logistics.portal_my_calculator",
            self._calculator_values(form=kw),
        )

    @http.route(['/my/calculator/calculate'], type='http', auth="public",
                website=True, methods=['POST'])
    def portal_my_calculator_calculate(self, **post):
        seller = self._calculator_seller()
        method = self._calculator_method(seller)
        if method == 'indiapost':
            quote, error = self._calculator_quote_indiapost(post, seller)
        else:
            quote, error = self._calculator_quote_slabs(post, seller)
        return request.render(
            "keralariders_logistics.portal_my_calculator",
            self._calculator_values(form=post, quote=quote, error=error),
        )

    def _calculator_quote_indiapost(self, post, seller):
        """Live India Post rate for the submitted package and product."""
        if self._calculator_throttled():
            return None, _(
                'Too many rate lookups from this session. Please wait a few '
                'minutes and try again.'
            )
        try:
            weight = float(post.get('weight') or 0)
            length = float(post.get('length_cm') or 0)
            breadth = float(post.get('breadth_cm') or 0)
            height = float(post.get('height_cm') or 0)
            insurance = float(post.get('insurance_value') or 0)
        except (TypeError, ValueError):
            return None, _(
                'Please enter the weight and all three dimensions as numbers.'
            )
        if weight <= 0:
            return None, _('Enter a weight greater than zero.')
        if not (length > 0 and breadth > 0 and height > 0):
            return None, _(
                'India Post prices on size as well as weight, so length, '
                'breadth and height are all required.'
            )

        quote = request.env['logistics.indiapost.tariff'].sudo().quote_safe(
            post.get('origin_pincode') or (seller.zip if seller else ''),
            post.get('dest_pincode'),
            article_type=self._ip_article_type_from_post(post),
            weight_kg=weight,
            length_cm=length,
            breadth_cm=breadth,
            height_cm=height,
            insurance_value=insurance,
            pod=bool(post.get('vas_pod')),
            reg=bool(post.get('vas_reg')),
            ack=bool(post.get('vas_ack')),
            otp=bool(post.get('vas_otp')),
        )
        if not quote.get('ok'):
            return None, quote.get('error')
        return quote, None

    def _calculator_quote_slabs(self, post, seller):
        """The original KeralaXpress weight slab price, by district pair."""
        try:
            weight = float(post.get('weight') or 0)
            origin_district_id = int(post.get('origin_district_id'))
            dest_district_id = int(post.get('dest_district_id'))
        except (TypeError, ValueError):
            return None, _(
                'Please enter a valid weight and select both districts.'
            )
        if weight <= 0:
            return None, _('Enter a weight greater than zero.')

        District = request.env['logistics.district'].sudo()
        origin = District.browse(origin_district_id)
        dest = District.browse(dest_district_id)
        if not (origin.exists() and dest.exists()):
            return None, _('Please select both districts.')

        same_district = origin_district_id == dest_district_id
        package_id = seller.delivery_package_id.id \
            if seller and seller.delivery_package_id else None
        try:
            charge = request.env['logistics.delivery.charges'].sudo() \
                .calculate_delivery_charge(weight, same_district,
                                           package_id=package_id)
        except UserError as error:
            return None, str(error)
        except Exception:  # pragma: no cover - defensive for a public route
            return None, _(
                'Unable to calculate the delivery charge. Please try again.'
            )
        return {
            'ok': True,
            'method': 'own_network',
            'origin_name': origin.name,
            'dest_name': dest.name,
            'same_district': same_district,
            'weight_display': self._format_calculator_weight(weight),
            'total_payable': charge,
        }, None

    @http.route(['/my/deliveries', '/my/deliveries/page/<int:page>'], type='http', auth="user", website=True)
    def portal_my_deliveries(self, page=1, date_begin=None, date_end=None, sortby=None, **kw):
        delivery_executive = request.env['logistics.delivery.executive'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
        if not delivery_executive:
            return request.redirect('/my')
            
        Shipment = request.env['logistics.shipment'].sudo()
        domain = delivery_executive._my_tasks_domain() + [
                ('state', 'not in', ('delivered', 'cancelled', 'returned')),
            ]
            
        searchbar_sortings = {
            'date': {'label': _('Newest'), 'order': 'create_date desc, id desc'},
            'name': {'label': _('Reference'), 'order': 'name'},
        }
        if not sortby:
            sortby = 'date'
        order = searchbar_sortings[sortby]['order']

        shipment_count = Shipment.search_count(domain)
        pager = portal_pager(
            url="/my/deliveries",
            url_args={'date_begin': date_begin, 'date_end': date_end, 'sortby': sortby},
            total=shipment_count,
            page=page,
            step=self._items_per_page
        )
        
        shipments = Shipment.search(domain, order=order, limit=self._items_per_page, offset=pager['offset'])
        
        values = {
            'shipments': shipments,
            'page_name': 'deliveries',
            'pager': pager,
            'default_url': '/my/deliveries',
            'searchbar_sortings': searchbar_sortings,
            'sortby': sortby,
            'delivery_executive': delivery_executive,
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_deliveries", values)

    @http.route(['/my/delivery/<int:shipment_id>'], type='http', auth="user", website=True)
    def portal_my_delivery_detail(self, shipment_id=None, **kw):
        delivery_executive = request.env['logistics.delivery.executive'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
        if not delivery_executive:
            return request.redirect('/my')
            
        shipment = request.env['logistics.shipment'].sudo().search([('id', '=', shipment_id)], limit=1)
        if not shipment:
            return request.redirect('/my/deliveries')

        # Prefer claim page when DE can self-assign an *unassigned* hub package.
        # Soft-assigned pending accept stays on detail with Accept / Reject.
        if (
            shipment.can_de_self_assign(delivery_executive)
            and not kw.get('view')
            and not shipment.is_pending_de_acceptance(delivery_executive)
        ):
            return request.redirect(f'/my/delivery/{shipment.id}/claim')

        shipment._sync_active_leg()
        active_leg = shipment.active_leg_id
        hubs = shipment.get_portal_drop_hub_ids()
        preferred_drop_hub = shipment.get_preferred_portal_drop_hub()
        pending_accept = shipment.is_pending_de_acceptance(delivery_executive)
        pickup_drop_blocked = shipment.is_pickup_drop_blocked()

        tracking_events = shipment.get_tracking_timeline(newest_first=False)

        values = {
            'shipment': shipment,
            'page_name': 'deliveries',
            'hubs': hubs,
            'delivery_executive': delivery_executive,
            'can_self_assign': shipment.can_de_self_assign(delivery_executive),
            'pending_accept': pending_accept,
            'pickup_drop_blocked': pickup_drop_blocked,
            'active_leg': active_leg,
            'active_leg_label': shipment.get_active_leg_label(),
            'preferred_drop_hub': preferred_drop_hub,
            'can_skip_hub': shipment.can_skip_hub_local_delivery(delivery_executive),
            'is_hub_transfer': bool(active_leg and active_leg.operation_type == 'hub_transfer'),
            'show_pickup_address': shipment.is_pickup_address_context(delivery_executive),
            'tracking_events': tracking_events,
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_delivery_detail", values)

    @http.route(['/my/delivery/<int:shipment_id>/claim'], type='http', auth="user", website=True, methods=['GET', 'POST'])
    def portal_my_delivery_claim(self, shipment_id=None, **post):
        delivery_executive = request.env['logistics.delivery.executive'].sudo().search(
            [('user_id', '=', request.env.user.id)], limit=1
        )
        if not delivery_executive:
            return request.redirect('/my')
        shipment = request.env['logistics.shipment'].sudo().browse(shipment_id)
        if not shipment.exists():
            request.session['error'] = "Shipment not found."
            return request.redirect('/my/deliveries')

        if request.httprequest.method == 'POST':
            try:
                shipment.action_de_self_assign(
                    de=delivery_executive,
                    scanned_code=post.get('scanned_code') or shipment.name,
                    note=post.get('note'),
                )
                request.session['success'] = f"Claimed {shipment.name}. Package is now in your custody."
                return request.redirect(f'/my/delivery/{shipment.id}')
            except UserError as e:
                request.session['error'] = str(e)
                return request.redirect(f'/my/delivery/{shipment.id}/claim')

        if not shipment.can_de_self_assign(delivery_executive):
            request.session['error'] = "You are not eligible to claim this shipment."
            return request.redirect(f'/my/delivery/{shipment.id}?view=1')

        shipment._sync_active_leg()
        leg = shipment._get_claimable_leg(delivery_executive)
        values = {
            'shipment': shipment,
            'leg': leg,
            'page_name': 'deliveries',
            'delivery_executive': delivery_executive,
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_delivery_claim", values)

    @http.route(
        ['/my/delivery/<int:shipment_id>/accept_assignment'],
        type='http', auth="user", website=True, methods=['POST'],
    )
    def portal_my_delivery_accept_assignment(self, shipment_id=None, **post):
        delivery_executive = request.env['logistics.delivery.executive'].sudo().search(
            [('user_id', '=', request.env.user.id)], limit=1
        )
        if not delivery_executive:
            return request.redirect('/my')
        shipment = request.env['logistics.shipment'].sudo().browse(shipment_id)
        if not shipment.exists():
            request.session['error'] = "Shipment not found."
            return request.redirect('/my/deliveries')
        try:
            shipment.action_de_accept_assignment(
                de=delivery_executive,
                scanned_code=post.get('scanned_code') or shipment.name,
                note=post.get('note'),
            )
            request.session['success'] = (
                f"Accepted {shipment.name}. Package is now in your custody."
            )
        except UserError as e:
            request.session['error'] = str(e)
        return request.redirect(f'/my/delivery/{shipment.id}')

    @http.route(
        ['/my/delivery/<int:shipment_id>/reject_assignment'],
        type='http', auth="user", website=True, methods=['POST'],
    )
    def portal_my_delivery_reject_assignment(self, shipment_id=None, **post):
        delivery_executive = request.env['logistics.delivery.executive'].sudo().search(
            [('user_id', '=', request.env.user.id)], limit=1
        )
        if not delivery_executive:
            return request.redirect('/my')
        shipment = request.env['logistics.shipment'].sudo().browse(shipment_id)
        if not shipment.exists():
            request.session['error'] = "Shipment not found."
            return request.redirect('/my/deliveries')
        try:
            shipment.action_de_reject_assignment(
                de=delivery_executive,
                note=post.get('note'),
            )
            request.session['success'] = (
                f"Rejected assignment for {shipment.name}. Hub keeps custody."
            )
            return request.redirect('/my/deliveries')
        except UserError as e:
            request.session['error'] = str(e)
            return request.redirect(f'/my/delivery/{shipment.id}')

    @http.route(['/my/delivery/<int:shipment_id>/mark_picked'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_delivery_mark_picked(self, shipment_id=None, **post):
        delivery_executive = request.env['logistics.delivery.executive'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
        if not delivery_executive:
            return request.redirect('/my')
        shipment = request.env['logistics.shipment'].sudo().browse(shipment_id)
        if not shipment.exists():
            request.session['error'] = "Shipment not found."
            return request.redirect('/my/deliveries')
        try:
            shipment.action_mark_picked(
                actor_de=delivery_executive,
                scanned_code=post.get('scanned_code') or shipment.name,
            )
            request.session['success'] = f"Shipment {shipment.name} marked as picked."
        except UserError as e:
            request.session['error'] = str(e)
        return request.redirect(f'/my/delivery/{shipment.id}')

    @http.route(['/my/delivery/<int:shipment_id>/drop_at_hub'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_delivery_drop_at_hub(self, shipment_id=None, **post):
        delivery_executive = request.env['logistics.delivery.executive'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
        if not delivery_executive:
            return request.redirect('/my')
        shipment = request.env['logistics.shipment'].sudo().browse(shipment_id)
        if not shipment.exists():
            request.session['error'] = "Shipment not found."
            return request.redirect('/my/deliveries')
        shipment._sync_active_leg()
        hub_id = int(post.get('hub_id') or 0)
        allowed = shipment.get_portal_drop_hub_ids()
        if hub_id:
            hub = allowed.filtered(lambda h: h.id == hub_id)[:1]
            if not hub:
                request.session['error'] = (
                    "Selected hub is not valid for this shipment. "
                    "Choose pickup hub, destination hub"
                    + (", or Thrissur main hub" if shipment._is_north_south_cross_zone() else "")
                    + "."
                )
                return request.redirect(f'/my/delivery/{shipment.id}')
        else:
            hub = shipment.get_preferred_portal_drop_hub()
        try:
            shipment.action_drop_at_hub(
                hub=hub,
                actor_de=delivery_executive,
                scanned_code=post.get('scanned_code') or shipment.name,
                note=post.get('note'),
            )
            request.session['success'] = f"Shipment {shipment.name} marked dropped at {hub.name}. Awaiting hub receive."
        except UserError as e:
            request.session['error'] = str(e)
        return request.redirect(f'/my/delivery/{shipment.id}')

    @http.route(['/my/delivery/<int:shipment_id>/skip_hub'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_delivery_skip_hub(self, shipment_id=None, **post):
        delivery_executive = request.env['logistics.delivery.executive'].sudo().search(
            [('user_id', '=', request.env.user.id)], limit=1
        )
        if not delivery_executive:
            return request.redirect('/my')
        shipment = request.env['logistics.shipment'].sudo().browse(shipment_id)
        if not shipment.exists():
            request.session['error'] = "Shipment not found."
            return request.redirect('/my/deliveries')
        try:
            shipment.action_skip_hub_local_delivery(
                actor_de=delivery_executive,
                scanned_code=post.get('scanned_code') or shipment.name,
                note=post.get('note'),
            )
            request.session['success'] = (
                f"Shipment {shipment.name} is now out for local delivery (hub skipped)."
            )
        except UserError as e:
            request.session['error'] = str(e)
        return request.redirect(f'/my/delivery/{shipment.id}')

    @http.route(['/my/cod_deposit'], type='http', auth="user", website=True, methods=['GET', 'POST'])
    def portal_my_cod_deposit(self, **post):
        """DE deposits COD cash holdings at a hub (DE cash → Hub cash)."""
        delivery_executive = request.env['logistics.delivery.executive'].sudo().search(
            [('user_id', '=', request.env.user.id)], limit=1
        )
        if not delivery_executive:
            return request.redirect('/my')
        Transfer = request.env['logistics.account.transfer'].sudo()
        cash_account = delivery_executive.default_cash_account_id
        undeposited = Transfer.browse()
        if cash_account:
            undeposited = Transfer.search([
                ('transfer_type', '=', 'cod_payment'),
                ('to_account_id', '=', cash_account.id),
                ('hub_deposit_transfer_id', '=', False),
            ], order='transfer_date desc, id desc')

        hubs = request.env['logistics.hub'].sudo().search([('active', '=', True), ('hub_type', '=', 'district')])

        if request.httprequest.method == 'POST':
            try:
                hub_id = int(post.get('hub_id') or 0)
                hub = request.env['logistics.hub'].sudo().browse(hub_id)
                if not hub.exists():
                    raise UserError("Please select a valid hub.")
                selected_ids = request.httprequest.form.getlist('payment_ids')
                payments = Transfer.browse([int(i) for i in selected_ids if i]).exists()
                if not payments:
                    payments = undeposited
                transfer = Transfer.action_create_hub_deposit(
                    de=delivery_executive,
                    hub=hub,
                    payment_transfers=payments,
                    note=post.get('note'),
                )
                request.session['success'] = (
                    f"Deposited {transfer.amount:.2f} at {hub.name} ({transfer.name})."
                )
                return request.redirect('/my/cod_deposit')
            except (UserError, ValueError) as e:
                request.session['error'] = str(e)
                return request.redirect('/my/cod_deposit')

        values = {
            'page_name': 'cod_deposit',
            'delivery_executive': delivery_executive,
            'cash_account': cash_account,
            'undeposited': undeposited,
            'undeposited_total': sum(undeposited.mapped('amount')),
            'hubs': hubs,
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_cod_deposit", values)

    @http.route(['/my/delivery/<int:shipment_id>/mark_delivered'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_delivery_mark_delivered(self, shipment_id=None, **post):
        delivery_executive = request.env['logistics.delivery.executive'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
        if not delivery_executive:
            return request.redirect('/my')
            
        shipment = request.env['logistics.shipment'].sudo().search([('id', '=', shipment_id)], limit=1)
        if not shipment:
            request.session['error'] = "Shipment not found."
            return request.redirect('/my/deliveries')
            
        if shipment.state in ('delivered', 'returned'):
            request.session['error'] = "Shipment is already completed."
            return request.redirect(f'/my/delivery/{shipment.id}')

        try:
            is_return = shipment.is_return_journey
            payment_method = None
            # COD collection only on outbound delivery — returns are free, no COD re-collect
            if not is_return and shipment.order_payment_type == 'cod':
                payment_method = post.get('cod_payment_method')
                if payment_method not in ['cash', 'upi']:
                    request.session['error'] = "Please select a valid COD payment method."
                    return request.redirect(f'/my/delivery/{shipment.id}')
                shipment.sudo().write({'cod_payment_method': payment_method})

            delivery_remarks = post.get('delivery_remarks') or ''
            shipment.action_mark_delivered(
                actor_de=delivery_executive,
                delivery_remarks=delivery_remarks,
            )
            if not is_return and shipment.order_payment_type == 'cod':
                shipment.sudo().action_create_payment_cod_from_portal(payment_method=payment_method)

            if is_return:
                request.session['success'] = (
                    f"Shipment {shipment.name} returned to seller successfully (free — no wallet charge)."
                )
            else:
                request.session['success'] = f"Shipment {shipment.name} marked as delivered successfully!"
            return request.redirect('/my/deliveries')
        except Exception as e:
            request.session['error'] = str(e)
            return request.redirect(f'/my/delivery/{shipment.id}')

    @http.route(
        ['/my/delivery/<int:shipment_id>/mark_delivery_failed'],
        type='http', auth="user", website=True, methods=['POST'],
    )
    def portal_my_delivery_mark_delivery_failed(self, shipment_id=None, **post):
        delivery_executive = request.env['logistics.delivery.executive'].sudo().search(
            [('user_id', '=', request.env.user.id)], limit=1
        )
        if not delivery_executive:
            return request.redirect('/my')
        shipment = request.env['logistics.shipment'].sudo().search(
            [('id', '=', shipment_id)], limit=1
        )
        if not shipment:
            request.session['error'] = "Shipment not found."
            return request.redirect('/my/deliveries')
        try:
            shipment.action_mark_delivery_failed(
                actor_de=delivery_executive,
                delivery_remarks=post.get('delivery_remarks') or '',
            )
            request.session['success'] = (
                f"Shipment {shipment.name} marked Delivery Failed (customer did not accept). "
                f"Return the package to the hub for Receive AWB."
            )
            return request.redirect(f'/my/delivery/{shipment.id}')
        except UserError as e:
            request.session['error'] = str(e)
            return request.redirect(f'/my/delivery/{shipment.id}')
        except Exception as e:
            request.session['error'] = str(e)
            return request.redirect(f'/my/delivery/{shipment.id}')

    # -------------------------------------------------------------------------
    # Hub Manager Portal
    # -------------------------------------------------------------------------
    def _get_managed_hubs(self):
        return request.env['logistics.hub'].sudo().search([
            ('manager_ids', 'in', request.env.user.ids),
            ('active', '=', True),
        ])

    def _hub_manager_sees_all_shipments(self, hubs):
        """Main hub (Thrissur) managers see every shipment system-wide."""
        return bool(hubs.filtered(lambda h: h.hub_type == 'main'))

    def _hub_all_shipments_domain(self, hubs):
        """Read-only list domain: related hubs, or unrestricted for main managers.

        Normal managers: source, destination, or current hub is managed
        (covers outbound and return journeys involving those hubs).
        Main hub managers: no hub filter.
        """
        if self._hub_manager_sees_all_shipments(hubs):
            return []
        return [
            '|', '|',
            ('source_hub_id', 'in', hubs.ids),
            ('destination_hub_id', 'in', hubs.ids),
            ('current_hub_id', 'in', hubs.ids),
        ]

    def _hub_pending_pickup_domain(self, hubs):
        """Shipments awaiting first pickup whose origin hub the user manages.

        India Post collects outbound articles from the seller directly, so they
        are not a KeralaXpress pickup and must stay out of this queue. A return
        journey is collected from the customer by a KeralaXpress executive
        whatever the carrier, so those remain listed.
        """
        return [
            ('source_hub_id', 'in', hubs.ids),
            ('state', 'in', ('pickup_requested', 'order_added')),
            '|',
            ('fulfilment_method', '!=', 'indiapost'),
            ('is_return_journey', '=', True),
        ]

    @http.route(['/my/hub', '/my/hub/'], type='http', auth="user", website=True)
    def portal_my_hub_home(self, **kw):
        hubs = self._get_managed_hubs()
        if not hubs:
            return request.redirect('/my')
        Shipment = request.env['logistics.shipment'].sudo()
        inventory_count = Shipment.search_count([
            ('custodian_type', '=', 'hub'),
            ('current_hub_id', 'in', hubs.ids),
        ])
        # DE custody at managed hubs, plus picked packages headed to source hub
        # (no DE "drop" required — hub manager Receive AWB takes custody).
        awaiting_receive = Shipment.search_count([
            '|',
            '&',
            ('current_hub_id', 'in', hubs.ids),
            ('custodian_type', '=', 'de'),
            ('state', 'in', ('picked', 'return_picked', 'in_transit', 'delivery_failed')),
            '&',
            ('source_hub_id', 'in', hubs.ids),
            ('custodian_type', '=', 'de'),
            ('current_hub_id', '=', False),
            ('state', 'in', ('picked', 'return_picked')),
        ])
        pending_pickup_domain = self._hub_pending_pickup_domain(hubs)
        pending_pickups = Shipment.search_count(pending_pickup_domain)
        unassigned_pickups = Shipment.search_count(
            pending_pickup_domain + [('pickup_executive_id', '=', False)]
        )
        related_domain = self._hub_all_shipments_domain(hubs)
        active_related = Shipment.search_count(related_domain + [
            ('state', 'not in', ('delivered', 'cancelled', 'returned')),
        ])
        values = {
            'page_name': 'hub',
            'hubs': hubs,
            'inventory_count': inventory_count,
            'awaiting_receive': awaiting_receive,
            'pending_pickups': pending_pickups,
            'unassigned_pickups': unassigned_pickups,
            'active_related_shipments': active_related,
            'sees_all_shipments': self._hub_manager_sees_all_shipments(hubs),
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_hub_home", values)

    @http.route(['/my/hub/cod', '/my/hub/cod/'], type='http', auth="user", website=True, methods=['GET', 'POST'])
    def portal_my_hub_cod(self, **post):
        """Hub manager: review DE deposits and bank cash to company."""
        hubs = self._get_managed_hubs()
        if not hubs:
            return request.redirect('/my')
        Transfer = request.env['logistics.account.transfer'].sudo()

        if request.httprequest.method == 'POST':
            try:
                hub_id = int(post.get('hub_id') or 0)
                hub = hubs.filtered(lambda h: h.id == hub_id)[:1]
                if not hub:
                    raise UserError("Please select one of your managed hubs.")
                selected_ids = request.httprequest.form.getlist('deposit_ids')
                deposits = Transfer.browse([int(i) for i in selected_ids if i]).exists()
                transfer = Transfer.action_create_hub_banking(
                    hub=hub,
                    deposit_transfers=deposits if deposits else None,
                    note=post.get('note'),
                )
                request.session['success'] = (
                    f"Banked {transfer.amount:.2f} from {hub.name} to company ({transfer.name})."
                )
                return request.redirect('/my/hub/cod')
            except (UserError, ValueError) as e:
                request.session['error'] = str(e)
                return request.redirect('/my/hub/cod')

        # Ensure cash accounts exist
        for hub in hubs:
            hub.get_or_create_cash_account()
        hub_accounts = hubs.mapped('cash_account_id')
        deposits = Transfer.search([
            ('transfer_type', '=', 'hub_deposit'),
            ('to_account_id', 'in', hub_accounts.ids),
        ], order='transfer_date desc, id desc', limit=100)
        unbanked = deposits.filtered(lambda d: not d.hub_banking_transfer_id)
        values = {
            'page_name': 'hub_cod',
            'hubs': hubs,
            'deposits': deposits,
            'unbanked': unbanked,
            'unbanked_total': sum(unbanked.mapped('amount')),
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_hub_cod", values)

    @http.route(['/my/hub/inventory', '/my/hub/inventory/page/<int:page>'], type='http', auth="user", website=True)
    def portal_my_hub_inventory(self, page=1, **kw):
        hubs = self._get_managed_hubs()
        if not hubs:
            return request.redirect('/my')
        Shipment = request.env['logistics.shipment'].sudo()
        domain = [
            ('custodian_type', '=', 'hub'),
            ('current_hub_id', 'in', hubs.ids),
        ]
        shipment_count = Shipment.search_count(domain)
        pager = portal_pager(
            url="/my/hub/inventory",
            total=shipment_count,
            page=page,
            step=self._items_per_page,
        )
        shipments = Shipment.search(domain, order='write_date desc', limit=self._items_per_page, offset=pager['offset'])
        all_executives = request.env['logistics.delivery.executive'].sudo().search([('active', '=', True)])
        # Per-shipment eligible DEs by active leg role (fallback: all)
        shipment_executives = {}
        for shipment in shipments:
            shipment._sync_active_leg()
            leg = shipment.active_leg_id
            if leg and leg.operation_type == 'pickup':
                eligible = all_executives.filtered(lambda d: shipment._de_eligible_for_operation(d, 'pickup'))
            elif leg and leg.operation_type == 'hub_transfer':
                eligible = all_executives.filtered(lambda d: shipment._de_eligible_for_operation(d, 'hub_transfer'))
            elif leg and leg.operation_type == 'delivery':
                eligible = all_executives.filtered(lambda d: shipment._de_eligible_for_operation(d, 'delivery'))
            else:
                eligible = all_executives
            shipment_executives[shipment.id] = eligible or all_executives
        values = {
            'page_name': 'hub_inventory',
            'hubs': hubs,
            'shipments': shipments,
            'executives': all_executives,
            'shipment_executives': shipment_executives,
            'pager': pager,
            'default_url': '/my/hub/inventory',
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_hub_inventory", values)

    @http.route(['/my/hub/receive'], type='http', auth="user", website=True, methods=['GET', 'POST'])
    def portal_my_hub_receive(self, **post):
        hubs = self._get_managed_hubs()
        if not hubs:
            return request.redirect('/my')
        if request.httprequest.method == 'POST':
            awb = (post.get('awb') or '').strip()
            hub_id = int(post.get('hub_id') or 0)
            hub = hubs.filtered(lambda h: h.id == hub_id)[:1] or hubs[:1]
            shipment = request.env['logistics.shipment'].sudo().search([('name', '=', awb)], limit=1)
            if not shipment:
                request.session['error'] = f"No shipment found for AWB '{awb}'."
                return request.redirect('/my/hub/receive')
            try:
                shipment.action_hub_receive(hub=hub, scanned_code=awb)
                request.session['success'] = f"Received {shipment.name} at {hub.name}."
                return request.redirect('/my/hub/inventory')
            except UserError as e:
                request.session['error'] = str(e)
                return request.redirect('/my/hub/receive')
        values = {
            'page_name': 'hub_receive',
            'hubs': hubs,
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_hub_receive", values)

    @http.route(['/my/hub/dispatch/<int:shipment_id>'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_hub_dispatch(self, shipment_id=None, **post):
        """Hub inventory: soft-assign DE (custody stays at hub until DE accepts)."""
        hubs = self._get_managed_hubs()
        if not hubs:
            return request.redirect('/my')
        shipment = request.env['logistics.shipment'].sudo().browse(shipment_id)
        if not shipment.exists() or shipment.current_hub_id not in hubs:
            request.session['error'] = "Shipment not in your hub inventory."
            return request.redirect('/my/hub/inventory')
        de_id = int(post.get('delivery_executive_id') or 0)
        de = request.env['logistics.delivery.executive'].sudo().browse(de_id)
        if not de.exists():
            request.session['error'] = "Please select a delivery executive."
            return request.redirect('/my/hub/inventory')
        try:
            shipment.action_assign_delivery_executive(delivery_executive=de)
            request.session['success'] = (
                f"Assigned {shipment.name} to {de.name} (pending accept). "
                f"Package stays in hub inventory until the DE accepts."
            )
        except UserError as e:
            request.session['error'] = str(e)
        return request.redirect('/my/hub/inventory')

    @http.route(['/my/hub/return_previous/<int:shipment_id>'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_hub_return_previous(self, shipment_id=None, **post):
        """Hub: open reverse transfer to source hub after delivery failure."""
        hubs = self._get_managed_hubs()
        if not hubs:
            return request.redirect('/my')
        shipment = request.env['logistics.shipment'].sudo().browse(shipment_id)
        if not shipment.exists() or shipment.current_hub_id not in hubs:
            request.session['error'] = "Shipment not in your hub inventory."
            return request.redirect('/my/hub/inventory')
        try:
            shipment.action_hub_return_to_previous_hub()
            request.session['success'] = (
                f"Opened return-to-source transfer for {shipment.name}. "
                f"Assign a DE for the hub transfer."
            )
        except UserError as e:
            request.session['error'] = str(e)
        return request.redirect('/my/hub/inventory')

    @http.route(['/my/hub/return_seller/<int:shipment_id>'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_hub_return_seller(self, shipment_id=None, **post):
        """Hub: start free return to seller from a failed delivery at hub."""
        hubs = self._get_managed_hubs()
        if not hubs:
            return request.redirect('/my')
        shipment = request.env['logistics.shipment'].sudo().browse(shipment_id)
        if not shipment.exists() or shipment.current_hub_id not in hubs:
            request.session['error'] = "Shipment not in your hub inventory."
            return request.redirect('/my/hub/inventory')
        try:
            shipment.action_hub_return_to_seller()
            request.session['success'] = (
                f"Started free return to seller for {shipment.name}. "
                f"Assign a DE for the next hop."
            )
        except UserError as e:
            request.session['error'] = str(e)
        return request.redirect('/my/hub/inventory')

    @http.route(['/my/hub/pickups', '/my/hub/pickups/page/<int:page>'], type='http', auth="user", website=True)
    def portal_my_hub_pickups(self, page=1, **kw):
        """List shipments awaiting pickup for managed origin hubs; assign pickup DE."""
        hubs = self._get_managed_hubs()
        if not hubs:
            return request.redirect('/my')
        Shipment = request.env['logistics.shipment'].sudo()
        domain = self._hub_pending_pickup_domain(hubs)
        shipment_count = Shipment.search_count(domain)
        pager = portal_pager(
            url="/my/hub/pickups",
            total=shipment_count,
            page=page,
            step=self._items_per_page,
        )
        shipments = Shipment.search(
            domain,
            order='pickup_requested_on desc, write_date desc, id desc',
            limit=self._items_per_page,
            offset=pager['offset'],
        )
        unassigned_count = Shipment.search_count(
            domain + [('pickup_executive_id', '=', False)]
        )
        shipment_executives = {}
        for shipment in shipments:
            try:
                eligible = shipment._get_eligible_pickup_executives()
            except (UserError, ValueError):
                # Fall back to all active pickup-capable DEs rather than 500 the page
                eligible = request.env['logistics.delivery.executive'].sudo().search(
                    [('active', '=', True)]
                ).filtered(lambda d: shipment._de_eligible_for_operation(d, 'pickup'))
            # Always include current assignee so re-assign UI can show them
            if shipment.pickup_executive_id and shipment.pickup_executive_id not in eligible:
                eligible = shipment.pickup_executive_id | eligible
            shipment_executives[shipment.id] = eligible
        values = {
            'page_name': 'hub_pickups',
            'hubs': hubs,
            'shipments': shipments,
            'shipment_executives': shipment_executives,
            'unassigned_count': unassigned_count,
            'pager': pager,
            'default_url': '/my/hub/pickups',
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_hub_pickups", values)

    @http.route(['/my/hub/pickups/assign/<int:shipment_id>'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_hub_assign_pickup(self, shipment_id=None, **post):
        hubs = self._get_managed_hubs()
        if not hubs:
            return request.redirect('/my')
        shipment = request.env['logistics.shipment'].sudo().browse(shipment_id)
        if not shipment.exists() or shipment.source_hub_id not in hubs:
            request.session['error'] = "Shipment is not awaiting pickup for your hub."
            return request.redirect('/my/hub/pickups')
        if not shipment._needs_keralaxpress_pickup():
            request.session['error'] = (
                f"{shipment.name} travels by India Post, which collects it "
                f"directly from the seller. There is no KeralaXpress pickup to "
                f"assign."
            )
            return request.redirect('/my/hub/pickups')
        if shipment.state not in ('pickup_requested', 'order_added'):
            request.session['error'] = (
                f"Shipment {shipment.name} is no longer awaiting pickup "
                f"(status: {shipment.state})."
            )
            return request.redirect('/my/hub/pickups')
        try:
            de_id = int(post.get('delivery_executive_id') or 0)
        except (TypeError, ValueError):
            request.session['error'] = "Please select a pickup delivery executive."
            return request.redirect('/my/hub/pickups')
        de = request.env['logistics.delivery.executive'].sudo().browse(de_id)
        if not de.exists():
            request.session['error'] = "Please select a pickup delivery executive."
            return request.redirect('/my/hub/pickups')
        try:
            shipment.action_assign_pickup_executive(de)
            request.session['success'] = f"Assigned pickup of {shipment.name} to {de.name}."
        except UserError as e:
            request.session['error'] = str(e)
        return request.redirect('/my/hub/pickups')

    @http.route(
        ['/my/hub/shipments', '/my/hub/shipments/page/<int:page>'],
        type='http', auth="user", website=True,
    )
    def portal_my_hub_shipments(self, page=1, filter_status=None, search=None, **kw):
        """Read-only shipments list for hub managers (no assign/dispatch/receive)."""
        hubs = self._get_managed_hubs()
        if not hubs:
            return request.redirect('/my')
        Shipment = request.env['logistics.shipment'].sudo()
        domain = self._hub_all_shipments_domain(hubs)
        sees_all = self._hub_manager_sees_all_shipments(hubs)

        filter_status = (filter_status or '').strip() or None
        search = (search or '').strip() or None
        if filter_status:
            valid_states = {s[0] for s in Shipment._fields['state'].selection}
            if filter_status in valid_states:
                domain = domain + [('state', '=', filter_status)]
            else:
                filter_status = None
        if search:
            domain = domain + [('name', 'ilike', search)]

        shipment_count = Shipment.search_count(domain)
        pager = portal_pager(
            url="/my/hub/shipments",
            url_args={
                'filter_status': filter_status or None,
                'search': search or None,
            },
            total=shipment_count,
            page=page,
            step=self._items_per_page,
        )
        shipments = Shipment.search(
            domain,
            order='create_date desc, id desc',
            limit=self._items_per_page,
            offset=pager['offset'],
        )
        status_options = [
            (key, Shipment._PUBLIC_STATUS_LABELS.get(key) or label)
            for key, label in Shipment._fields['state'].selection
        ]
        values = {
            'page_name': 'hub_shipments',
            'hubs': hubs,
            'shipments': shipments,
            'shipment_count': shipment_count,
            'sees_all_shipments': sees_all,
            'filter_status': filter_status,
            'search': search or '',
            'status_options': status_options,
            'pager': pager,
            'default_url': '/my/hub/shipments',
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_hub_shipments", values)

    @http.route(['/my/cod_settlements', '/my/cod_settlements/page/<int:page>'], type='http', auth="user", website=True)
    def portal_my_cod_settlements(self, page=1, **kw):
        values = self._prepare_portal_layout_values()
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].sudo().search([('partner_id', '=', partner.id)], limit=1)
        
        if not seller:
            return request.redirect('/my')

        Transfer = request.env['logistics.account.transfer'].sudo()
        domain = [
            ('related_seller_id', '=', seller.id),
            ('transfer_type', 'in', ['cod_payment', 'cod_clearance', 'cod_withdrawal', 'other']),
            ('state', 'in', ['draft', 'posted']),
        ]
        
        # count for pager
        transfer_count = Transfer.search_count(domain)
        # pager
        pager = portal_pager(
            url="/my/cod_settlements",
            url_args={},
            total=transfer_count,
            page=page,
            step=20
        )
        
        # content according to pager
        transfers = Transfer.search(domain, order='transfer_date desc, id desc', limit=20, offset=pager['offset'])
        
        cod_balance = Transfer.get_seller_cod_pending_balance(seller)
        withdrawable = Transfer.get_seller_cod_withdrawable_balance(seller)
        draft_withdrawals = Transfer.search([
            ('related_seller_id', '=', seller.id),
            ('transfer_type', '=', 'cod_withdrawal'),
            ('state', '=', 'draft'),
        ], order='transfer_date desc, id desc')
        
        # Recent Settlements: posted clearances + withdrawals (+ legacy other payouts)
        recent_clearances = Transfer.search([
            ('related_seller_id', '=', seller.id),
            ('transfer_type', 'in', ['cod_clearance', 'cod_withdrawal', 'other']),
            ('state', '=', 'posted'),
        ], order='transfer_date desc, id desc', limit=5)

        has_bank_details = bool(
            seller.bank_account_name and seller.bank_account_number and seller.bank_ifsc
        )
        
        values.update({
            'transfers': transfers,
            'page_name': 'cod_settlements',
            'pager': pager,
            'default_url': '/my/cod_settlements',
            'cod_balance': cod_balance,
            'withdrawable_balance': withdrawable,
            'draft_withdrawals': draft_withdrawals,
            'recent_clearances': recent_clearances,
            'seller': seller,
            'has_bank_details': has_bank_details,
            'currency_id': seller.currency_id or request.env.company.currency_id,
            'cod_type_label': self._cod_settlement_seller_type_label,
            'cod_type_badge': self._cod_settlement_seller_type_badge,
            'success': request.session.pop('success', None),
            'error': request.session.pop('error', None),
        })
        
        return request.render("keralariders_logistics.portal_my_cod_settlements", values)

    @http.route(['/my/cod_settlements/withdraw'], type='http', auth="user", website=True, methods=['POST'])
    def portal_cod_withdrawal_request(self, **post):
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].sudo().search([('partner_id', '=', partner.id)], limit=1)
        if not seller:
            return request.redirect('/my')
        try:
            amount = float(post.get('amount') or 0)
            transfer = request.env['logistics.account.transfer'].sudo().action_create_cod_withdrawal(
                seller=seller,
                amount=amount,
            )
            request.session['success'] = _(
                "COD withdrawal request %(ref)s for %(amount)s submitted. "
                "It will remain in draft until a logistics admin approves it.",
                ref=transfer.name,
                amount=transfer.currency_id.format(transfer.amount) if transfer.currency_id else transfer.amount,
            )
        except (UserError, AccessError, ValueError) as e:
            request.session['error'] = str(e)
        return request.redirect('/my/cod_settlements')

    # -------------------------------------------------------------------------
    # Seller REST API keys and documentation
    # -------------------------------------------------------------------------
    def _portal_api_base_url(self):
        root = (request.httprequest.url_root or '').rstrip('/')
        return root or request.env['ir.config_parameter'].sudo().get_param('web.base.url')

    def _portal_api_examples(self, base_url):
        auth = (
            '  -H "X-Api-Key: YOUR_API_KEY" \\\n'
            '  -H "X-Api-Secret: YOUR_API_SECRET"'
        )
        return {
            'wallet': (
                'curl -s %s/api/v1/seller/wallet \\\n%s'
            ) % (base_url, auth),
            'rates': (
                'curl -s -X POST %s/api/v1/seller/rates \\\n%s \\\n'
                '  -H "Content-Type: application/json" \\\n'
                '  -d \'{"origin_pincode":"682001","destination_pincode":"695001","weight_kg":1.5}\''
            ) % (base_url, auth),
            'pincode': (
                'curl -s %s/api/v1/seller/pincodes/695001 \\\n%s'
            ) % (base_url, auth),
            'create': (
                'curl -s -X POST %s/api/v1/seller/shipments \\\n%s \\\n'
                '  -H "Content-Type: application/json" \\\n'
                '  -H "Idempotency-Key: order-12345" \\\n'
                '  -d \'{"customer_name":"Jane Doe","customer_phone":"9876543210",'
                '"customer_address":"12 MG Road, Thiruvananthapuram",'
                '"destination_pincode":"695001","weight_kg":1.5,'
                '"item_description":"Clothing","payment_type":"prepaid","book":true}\''
            ) % (base_url, auth),
            'list': (
                'curl -s "%s/api/v1/seller/shipments?page=1&page_size=20" \\\n%s'
            ) % (base_url, auth),
            'get': (
                'curl -s %s/api/v1/seller/shipments/AWBNUMBER \\\n%s'
            ) % (base_url, auth),
            'track': (
                'curl -s %s/api/v1/seller/shipments/AWBNUMBER/track \\\n%s'
            ) % (base_url, auth),
            'pickup': (
                'curl -s -X POST %s/api/v1/seller/shipments/AWBNUMBER/pickup \\\n%s'
            ) % (base_url, auth),
            'return': (
                'curl -s -X POST %s/api/v1/seller/shipments/AWBNUMBER/return \\\n%s'
            ) % (base_url, auth),
        }

    @http.route(['/my/api'], type='http', auth='user', website=True)
    def portal_my_api(self, **kw):
        seller = self._portal_seller()
        if not seller:
            return request.redirect('/my')
        Credential = request.env['logistics.seller.api.credential'].sudo()
        credentials = Credential.search([
            ('seller_id', '=', seller.id),
        ], order='create_date desc')
        active = credentials.filtered(lambda c: c.state == 'active')[:1]
        base_url = self._portal_api_base_url()
        values = {
            'page_name': 'api',
            'seller': seller,
            'credentials': credentials,
            'active_credential': active,
            'base_url': base_url,
            'examples': self._portal_api_examples(base_url),
            'secret_once': request.session.pop('seller_api_secret_once', None),
            'key_once': request.session.pop('seller_api_key_once', None),
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
            'daily_limit': Credential._daily_limit(),
        }
        return request.render('keralariders_logistics.portal_my_api', values)

    @http.route(['/my/api/keys/generate'], type='http', auth='user', website=True, methods=['POST'])
    def portal_my_api_generate(self, **post):
        seller = self._portal_seller()
        if not seller:
            return request.redirect('/my')
        try:
            credential, secret = request.env['logistics.seller.api.credential'].sudo().generate_for_seller(seller)
            request.session['seller_api_secret_once'] = secret
            request.session['seller_api_key_once'] = credential.api_key
            request.session['success'] = _(
                'API credentials generated. Copy the secret now — it will not be shown again.'
            )
        except Exception as exc:
            request.session['error'] = str(exc)
        return request.redirect('/my/api')

    @http.route(['/my/api/keys/disable'], type='http', auth='user', website=True, methods=['POST'])
    def portal_my_api_disable(self, **post):
        seller = self._portal_seller()
        if not seller:
            return request.redirect('/my')
        try:
            cred_id = int(post.get('credential_id') or 0)
        except (TypeError, ValueError):
            cred_id = 0
        credential = request.env['logistics.seller.api.credential'].sudo().search([
            ('id', '=', cred_id),
            ('seller_id', '=', seller.id),
            ('state', '=', 'active'),
        ], limit=1)
        if credential:
            credential.action_disable()
            request.session['success'] = _('API key disabled. Existing integrations will stop working.')
        else:
            request.session['error'] = _('No active API key found.')
        return request.redirect('/my/api')

