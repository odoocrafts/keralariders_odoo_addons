from odoo import http, fields, _
from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager
from odoo.http import request
from odoo.exceptions import UserError

class LogisticsPortal(CustomerPortal):
    
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
                
            cod_transfers = request.env['logistics.account.transfer'].sudo().search([
                ('related_seller_id', '=', seller.id),
                ('transfer_type', 'in', ['cod_payment', 'cod_clearance'])
            ])
            cod_balance_val = sum(t.amount if t.transfer_type == 'cod_payment' else -t.amount for t in cod_transfers)
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
                
                return request.render("keralariders_logistics.portal_my_wallet_recharge_pay", {
                    'amount': amount,
                    'qr_url': qr_url,
                    'wallet': wallet,
                    'page_name': 'wallet',
                })
        return request.redirect('/my/wallet')

    @http.route(['/my/wallet/recharge/confirm'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_wallet_recharge_confirm(self, **post):
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        if seller:
            wallet = request.env['logistics.wallet'].search([('seller_id', '=', seller.id)], limit=1)
            amount = float(post.get('amount', 0))
            if amount > 0 and wallet:
                request.env['logistics.wallet.recharge.request'].create({
                    'seller_id': seller.id,
                    'wallet_id': wallet.id,
                    'requested_amount': amount,
                })
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

    @http.route(['/my/shipments/new'], type='http', auth="user", website=True)
    def portal_my_shipments_new(self, **kw):
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        if not seller:
            return request.redirect('/my')
            
        districts = request.env['logistics.district'].sudo().search([])
        states = request.env['res.country.state'].sudo().search([('country_id', '=', request.env.company.country_id.id)])
        
        values = {
            'page_name': 'shipment_new',
            'seller': seller,
            'districts': districts,
            'states': states,
            'error': request.session.pop('error', None),
        }
        return request.render("keralariders_logistics.portal_my_shipment_new", values)

    @http.route(['/my/shipments/create'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_shipments_create(self, **post):
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        if not seller:
            return request.redirect('/my')
            
        try:
            total_weight = float(post.get('total_weight') or 0)
            if total_weight <= 0:
                raise UserError("Weight must be greater than 0.")
                
            shipment_vals = {
                'seller_id': seller.id,
                'shipping_to_name': post.get('shipping_to_name'),
                'shipping_to_address': post.get('shipping_to_address'),
                'shipping_to_zip': post.get('shipping_to_zip'),
                'shipping_to_district_id': int(post.get('shipping_to_district_id')) if post.get('shipping_to_district_id') else False,
                'shipping_to_state_id': int(post.get('shipping_to_state_id')) if post.get('shipping_to_state_id') else False,
                'shipping_to_mobile': post.get('shipping_to_mobile'),
                'item_description': post.get('item_description'),
                'total_weight': total_weight,
                'order_payment_type': post.get('order_payment_type', 'prepaid'),
                'total_order_value': float(post.get('total_order_value') or 0),
                'billing_same_as_shipping': True,
                'state': 'order_added',
            }
            shipment = request.env['logistics.shipment'].sudo().create(shipment_vals)
            
            if shipment.order_payment_type == 'cod':
                shipment.cod_amount = shipment.total_order_value
                
            request.session['success'] = f"Shipment '{shipment.name}' saved as Draft!"
            return request.redirect('/my/shipments')
            
        except Exception as e:
            request.session['error'] = str(e)
            return request.redirect('/my/shipments/new')
            
    @http.route(['/my/shipments/bulk_upload/template'], type='http', auth="user", website=True)
    def portal_my_shipments_bulk_upload_template(self, **kw):
        import csv
        import io
        
        output = io.StringIO()
        writer = csv.writer(output)
        headers = ['Customer Name', 'Phone Number', 'Address', 'Pincode', 'Weight (kg)', 'Item Description', 'Payment Type (prepaid/cod)', 'Total Order Value']
        writer.writerow(headers)
        
        # Add sample rows to help the user
        writer.writerow(['John Doe', '9876543210', '123 Main St, Apt 4B', '682001', '1.5', 'Electronics', 'prepaid', '0'])
        writer.writerow(['Jane Smith', '9988776655', '456 Market Road', '695001', '2.0', 'Clothing', 'cod', '1500'])
        
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
            
        try:
            import csv
            import io
            
            file_content = csv_file.read().decode('utf-8')
            csv_reader = csv.DictReader(io.StringIO(file_content))
            
            success_count = 0
            failed_count = 0
            
            order = request.env['logistics.order'].sudo().create({
                'seller_id': seller.id,
                'pickup_date': pickup_date,
            })
            
            for row in csv_reader:
                customer_name = row.get('Customer Name')
                phone = row.get('Phone Number')
                address = row.get('Address')
                pincode = row.get('Pincode')
                weight_str = row.get('Weight (kg)')
                description = row.get('Item Description')
                payment_type = row.get('Payment Type (prepaid/cod)', '').strip().lower()
                order_value_str = row.get('Total Order Value', '0')
                
                if not all([customer_name, phone, address, pincode, weight_str, description]):
                    failed_count += 1
                    continue
                    
                try:
                    weight = float(weight_str)
                    order_value = float(order_value_str) if order_value_str else 0.0
                except ValueError:
                    failed_count += 1
                    continue
                    
                district_id = False
                state_id = False
                pincode_info = request.env['logistics.district'].sudo().get_district_from_pincode(pincode)
                if pincode_info and pincode_info.get('district_id'):
                    district_id = pincode_info['district_id'].id
                    state_id = pincode_info['district_id'].state_id.id
                else:
                    failed_count += 1
                    continue
                    
                if payment_type not in ['prepaid', 'cod']:
                    payment_type = 'prepaid'
                    
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
                }
                
                shipment = request.env['logistics.shipment'].sudo().create(shipment_vals)
                if shipment.order_payment_type == 'cod':
                    shipment.cod_amount = shipment.total_order_value
                success_count += 1
                
            if success_count == 0:
                order.sudo().unlink()
                request.session['error'] = "All rows failed validation. Order not created."
                return request.redirect('/my/orders/new')
                
            msg = f"Order created with {success_count} shipments."
            if failed_count > 0:
                msg += f" {failed_count} rows failed validation and were skipped."
                
            request.session['success'] = msg
            return request.redirect(f'/my/orders/{order.id}')
            
        except UnicodeDecodeError:
            request.session['error'] = "Error reading file. Please ensure it is a valid CSV file saved with UTF-8 encoding."
            return request.redirect('/my/orders/new')
        except Exception as e:
            request.session['error'] = f"Error processing file: {str(e)}"
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
            request.session['success'] = f"Pickup requested successfully for Order {order.name}. {order.total_charges} deducted from wallet."
        except Exception as e:
            request.session['error'] = str(e)
            
        return request.redirect(f'/my/orders/{order.id}')

    @http.route(['/my/shipments/request_return'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_shipments_request_return(self, **post):
        shipment_id = int(post.get('shipment_id', 0))
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].search([('partner_id', '=', partner.id)], limit=1)
        
        shipment = request.env['logistics.shipment'].search([
            ('id', '=', shipment_id), 
            ('seller_id', '=', seller.id),
            ('state', '=', 'delivered')
        ], limit=1)
        
        if not shipment:
            request.session['error'] = "Shipment not found or not in Delivered state."
            return request.redirect('/my/shipments')
            
        try:
            # Free reverse journey: customer pickup → hubs → seller (no wallet debit)
            shipment.sudo().action_request_return()
            request.session['success'] = (
                f"Free return requested for {shipment.name}. "
                f"A DE will pick up from the customer, move through hubs, and deliver back to you. "
                f"No wallet deduction."
            )
        except UserError as e:
            request.session['error'] = str(e)
        except Exception as e:
            request.session['error'] = str(e)
            
        return request.redirect('/my/shipments')

    @http.route(['/my/calculator'], type='http', auth="public", website=True)
    def portal_my_calculator(self, **kw):
        districts = request.env['logistics.district'].sudo().search([])
        return request.render("keralariders_logistics.portal_my_calculator", {
            'page_name': 'calculator',
            'districts': districts,
            'result': kw.get('result', None),
            'error': kw.get('error', None)
        })

    @http.route(['/my/calculator/calculate'], type='http', auth="public", website=True, methods=['POST'])
    def portal_my_calculator_calculate(self, **post):
        try:
            weight = float(post.get('weight', 0))
            origin_district_id = int(post.get('origin_district_id'))
            dest_district_id = int(post.get('dest_district_id'))
            
            same_district = (origin_district_id == dest_district_id)
            package_id = None
            if request.session.uid:
                seller = request.env['logistics.seller'].sudo().search([('user_id', '=', request.session.uid)], limit=1)
                if seller and seller.delivery_package_id:
                    package_id = seller.delivery_package_id.id
                    
            charge = request.env['logistics.delivery.charges'].sudo().calculate_delivery_charge(weight, same_district, package_id=package_id)
            
            return request.redirect(f'/my/calculator?result={charge}')
        except Exception as e:
            return request.redirect(f'/my/calculator?error={e}')

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

        # Prefer claim page when DE is eligible to self-assign
        if shipment.can_de_self_assign(delivery_executive) and not kw.get('view'):
            return request.redirect(f'/my/delivery/{shipment.id}/claim')

        hubs = request.env['logistics.hub'].sudo().search([('active', '=', True)])
        shipment._sync_active_leg()
        active_leg = shipment.active_leg_id
        preferred_drop_hub = False
        if active_leg and active_leg.operation_type == 'hub_transfer' and active_leg.to_hub_id:
            preferred_drop_hub = active_leg.to_hub_id
        elif active_leg and active_leg.operation_type == 'pickup' and active_leg.to_hub_id:
            preferred_drop_hub = active_leg.to_hub_id
        else:
            preferred_drop_hub = shipment.source_hub_id

        tracking_events = shipment.get_tracking_timeline(newest_first=False)

        values = {
            'shipment': shipment,
            'page_name': 'deliveries',
            'hubs': hubs,
            'delivery_executive': delivery_executive,
            'can_self_assign': shipment.can_de_self_assign(delivery_executive),
            'active_leg': active_leg,
            'active_leg_label': shipment.get_active_leg_label(),
            'preferred_drop_hub': preferred_drop_hub,
            'can_depart_hub': shipment.can_depart_from_hub(delivery_executive),
            'can_skip_hub': shipment.can_skip_hub_local_delivery(delivery_executive),
            'is_hub_transfer': bool(active_leg and active_leg.operation_type == 'hub_transfer'),
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
        hub_id = int(post.get('hub_id') or 0)
        hub = request.env['logistics.hub'].sudo().browse(hub_id) if hub_id else False
        if not hub:
            leg = shipment.active_leg_id
            if leg and leg.operation_type == 'hub_transfer' and leg.to_hub_id:
                hub = leg.to_hub_id
            elif leg and leg.operation_type == 'pickup' and leg.to_hub_id:
                hub = leg.to_hub_id
            else:
                hub = shipment.source_hub_id
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

    @http.route(['/my/delivery/<int:shipment_id>/depart_hub'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_delivery_depart_hub(self, shipment_id=None, **post):
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
            shipment.action_depart_from_hub(
                actor_de=delivery_executive,
                note=post.get('note'),
            )
            request.session['success'] = f"Departed hub for transfer of {shipment.name}."
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

    # -------------------------------------------------------------------------
    # Hub Manager Portal
    # -------------------------------------------------------------------------
    def _get_managed_hubs(self):
        return request.env['logistics.hub'].sudo().search([
            ('manager_ids', 'in', request.env.user.ids),
            ('active', '=', True),
        ])

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
        awaiting_receive = Shipment.search_count([
            ('current_hub_id', 'in', hubs.ids),
            ('custodian_type', '=', 'de'),
            ('state', 'in', ('picked', 'return_picked', 'in_transit')),
        ])
        values = {
            'page_name': 'hub',
            'hubs': hubs,
            'inventory_count': inventory_count,
            'awaiting_receive': awaiting_receive,
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
        for_delivery = post.get('for_delivery', '1') == '1'
        try:
            shipment.action_hub_dispatch(
                delivery_executive=de,
                hub=shipment.current_hub_id,
                for_delivery=for_delivery,
            )
            request.session['success'] = f"Dispatched {shipment.name} to {de.name}."
        except UserError as e:
            request.session['error'] = str(e)
        return request.redirect('/my/hub/inventory')

    @http.route(['/my/cod_settlements', '/my/cod_settlements/page/<int:page>'], type='http', auth="user", website=True)
    def portal_my_cod_settlements(self, page=1, **kw):
        values = self._prepare_portal_layout_values()
        partner = request.env.user.partner_id
        seller = request.env['logistics.seller'].sudo().search([('partner_id', '=', partner.id)], limit=1)
        
        if not seller:
            return request.redirect('/my')

        Transfer = request.env['logistics.account.transfer'].sudo()
        domain = [('related_seller_id', '=', seller.id), ('transfer_type', 'in', ['cod_payment', 'cod_clearance'])]
        
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
        
        # Pending balance
        all_transfers = Transfer.search(domain)
        cod_balance = sum(t.amount if t.transfer_type == 'cod_payment' else -t.amount for t in all_transfers)
        
        # Recent Clearances
        recent_clearances = Transfer.search([
            ('related_seller_id', '=', seller.id),
            ('transfer_type', '=', 'cod_clearance')
        ], order='transfer_date desc, id desc', limit=5)
        
        values.update({
            'transfers': transfers,
            'page_name': 'cod_settlements',
            'pager': pager,
            'default_url': '/my/cod_settlements',
            'cod_balance': cod_balance,
            'recent_clearances': recent_clearances,
            'seller': seller,
            'currency_id': seller.currency_id or request.env.company.currency_id,
        })
        
        return request.render("keralariders_logistics.portal_my_cod_settlements", values)
