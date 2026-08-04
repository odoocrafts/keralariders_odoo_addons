from odoo import http
from odoo.http import request
from odoo.exceptions import UserError
import werkzeug

class SellerSignup(http.Controller):

    @http.route('/seller/signup', type='http', auth="public", website=True, sitemap=False)
    def seller_signup_form(self, **kw):
        if not request.env.user._is_public():
            return request.redirect('/my/home')
            
        values = {
            'error': request.session.pop('error', None),
            'success': request.session.pop('success', None),
        }
        return request.render("keralariders_logistics.seller_signup_page", values)

    @http.route('/seller/signup/success', type='http', auth="public", website=True, sitemap=False)
    def seller_signup_success(self, **kw):
        return request.render("keralariders_logistics.seller_signup_success_page")

    @http.route('/seller/signup/submit', type='http', auth="public", website=True, methods=['POST'], csrf=True)
    def seller_signup_submit(self, **post):
        try:
            name = post.get('name')
            email = post.get('email')
            phone = post.get('phone')
            password = post.get('password')
            confirm_password = post.get('confirm_password')
            agreement = post.get('agreement')
            street = post.get('street')
            street2 = post.get('street2')
            city = post.get('city')
            zip_code = post.get('zip')
            tax_id = post.get('tax_id')
            avatar_base64 = post.get('avatar_base64')
            
            if not all([name, email, phone, password, confirm_password, street, city, zip_code]):
                raise UserError("All required fields must be filled.")
                
            if password != confirm_password:
                raise UserError("Passwords do not match.")
                
            if not agreement:
                raise UserError("You must agree to the Seller Terms & Conditions.")
                
            # Check if user already exists
            existing_user = request.env['res.users'].sudo().search([('login', '=', email)], limit=1)
            if existing_user:
                raise UserError("Another user is already registered using this email address.")
                
            # Process avatar if provided
            image_1920 = False
            if avatar_base64:
                try:
                    # avatar_base64 format: "data:image/jpeg;base64,/9j/4AAQSkZJR..."
                    header, data = avatar_base64.split(',', 1)
                    image_1920 = data
                except Exception:
                    pass
                    
            # Get district and state from pincode
            district_id = False
            state_id = False
            if zip_code:
                pincode_info = request.env['logistics.district'].sudo().get_district_from_pincode(zip_code)
                if pincode_info and pincode_info.get('district_id'):
                    district_id = pincode_info['district_id'].id
                    state_id = pincode_info['district_id'].state_id.id
                
            # Create user
            portal_group = request.env.ref('base.group_portal')
            user_vals = {
                'name': name,
                'login': email,
                'password': password,
                'group_ids': [(4, portal_group.id)]
            }
            if image_1920:
                user_vals['image_1920'] = image_1920
            
            user = request.env['res.users'].sudo().create(user_vals)
            
            # Create seller linked to the user's partner
            seller_vals = {
                'name': name,
                'email': email,
                'phone': phone,
                'street': street,
                'street2': street2,
                'city': city,
                'zip': zip_code,
                'tax_id': tax_id,
                'district_id': district_id,
                'state_id': state_id,
                'partner_id': user.partner_id.id
            }
            if image_1920:
                seller_vals['image_1920'] = image_1920
                
            seller = request.env['logistics.seller'].sudo().create(seller_vals)
            
            # Registration successful, redirect to success page
            return request.redirect('/seller/signup/success')
            
        except Exception as e:
            request.session['error'] = str(e)
            return request.redirect('/seller/signup')
