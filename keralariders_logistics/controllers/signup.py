from odoo import http, _
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

    @http.route('/seller/signup/submit', type='http', auth="public", website=True, methods=['POST'], csrf=True)
    def seller_signup_submit(self, **post):
        try:
            name = post.get('name')
            email = post.get('email')
            phone = post.get('phone')
            password = post.get('password')
            
            if not all([name, email, phone, password]):
                raise UserError(_("All fields are required."))
                
            # Check if user already exists
            existing_user = request.env['res.users'].sudo().search([('login', '=', email)], limit=1)
            if existing_user:
                raise UserError(_("Another user is already registered using this email address."))
                
            # Create user
            portal_group = request.env.ref('base.group_portal')
            user = request.env['res.users'].sudo().create({
                'name': name,
                'login': email,
                'password': password,
                'group_ids': [(4, portal_group.id)]
            })
            
            # Create seller linked to the user's partner
            seller = request.env['logistics.seller'].sudo().create({
                'name': name,
                'email': email,
                'phone': phone,
                'partner_id': user.partner_id.id
            })
            
            # Authenticate user manually
            request.session.authenticate(email, password)
            
            return request.redirect('/my/home')
            
        except Exception as e:
            request.session['error'] = str(e)
            return request.redirect('/seller/signup')
