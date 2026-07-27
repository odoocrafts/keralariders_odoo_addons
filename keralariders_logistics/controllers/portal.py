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
                
            values['charge_calculator'] = ' '

        delivery_executive = request.env['logistics.delivery.executive'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
        values['is_delivery_executive'] = bool(delivery_executive)
        
        if delivery_executive:
            assigned_shipment_count = request.env['logistics.shipment'].sudo().search_count([('delivery_executive_id', '=', delivery_executive.id), ('state', '!=', 'delivered')])
            values['assigned_shipment_count'] = str(assigned_shipment_count) if assigned_shipment_count > 0 else '0 '
        
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
            # Update state (Free of charge)
            shipment.sudo().write({
                'state': 'return_requested'
            })
            request.session['success'] = f"Return pickup requested successfully for {shipment.name}. This is free of charge."
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
            charge = request.env['logistics.delivery.charges'].sudo().calculate_delivery_charge(weight, same_district)
            
            return request.redirect(f'/my/calculator?result={charge}')
        except Exception as e:
            return request.redirect(f'/my/calculator?error={e}')

    @http.route(['/my/deliveries', '/my/deliveries/page/<int:page>'], type='http', auth="user", website=True)
    def portal_my_deliveries(self, page=1, date_begin=None, date_end=None, sortby=None, **kw):
        delivery_executive = request.env['logistics.delivery.executive'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
        if not delivery_executive:
            return request.redirect('/my')
            
        Shipment = request.env['logistics.shipment'].sudo()
        domain = [('delivery_executive_id', '=', delivery_executive.id), ('state', '!=', 'delivered')]
        
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
            
        shipment = request.env['logistics.shipment'].sudo().search([('id', '=', shipment_id), ('delivery_executive_id', '=', delivery_executive.id)], limit=1)
        if not shipment:
            return request.redirect('/my/deliveries')
            
        values = {
            'shipment': shipment,
            'page_name': 'deliveries',
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.portal_my_delivery_detail", values)

    @http.route(['/my/delivery/<int:shipment_id>/mark_delivered'], type='http', auth="user", website=True, methods=['POST'])
    def portal_my_delivery_mark_delivered(self, shipment_id=None, **post):
        delivery_executive = request.env['logistics.delivery.executive'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
        if not delivery_executive:
            return request.redirect('/my')
            
        shipment = request.env['logistics.shipment'].sudo().search([('id', '=', shipment_id), ('delivery_executive_id', '=', delivery_executive.id)], limit=1)
        if not shipment:
            request.session['error'] = "Shipment not found."
            return request.redirect('/my/deliveries')
            
        if shipment.state == 'delivered':
            request.session['error'] = "Shipment is already delivered."
            return request.redirect(f'/my/delivery/{shipment.id}')

        try:
            vals = {'state': 'delivered', 'actual_delivery_date': fields.Datetime.now()}
            if shipment.order_payment_type == 'cod':
                payment_method = post.get('cod_payment_method')
                if payment_method in ['cash', 'upi']:
                    vals['cod_payment_method'] = payment_method
                else:
                    request.session['error'] = "Please select a valid COD payment method."
                    return request.redirect(f'/my/delivery/{shipment.id}')
            # Delivery remarks
            delivery_remarks = post.get('delivery_remarks')
            vals['delivery_remarks'] = delivery_remarks or ''
            
            shipment.sudo().write(vals)
            request.session['success'] = f"Shipment {shipment.name} marked as delivered successfully!"
            return request.redirect('/my/deliveries')
        except Exception as e:
            request.session['error'] = str(e)
            return request.redirect(f'/my/delivery/{shipment.id}')
