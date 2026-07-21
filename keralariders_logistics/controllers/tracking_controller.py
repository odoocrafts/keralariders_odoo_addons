from odoo import http
from odoo.http import request

class TrackingController(http.Controller):

    @http.route(['/track'], type='http', auth="public", website=False, methods=['GET', 'POST'], csrf=False)
    def track_search(self, **post):
        error = None
        if request.httprequest.method == 'POST':
            awb = post.get('awb', '').strip()
            phone = post.get('phone', '').strip()
            if awb and phone:
                shipment = request.env['logistics.shipment'].sudo().search([
                    ('name', '=', awb),
                    ('shipping_to_mobile', '=', phone)
                ], limit=1)
                if shipment:
                    return request.redirect(f'/track/{shipment.tracking_token}')
                else:
                    error = "No shipment found with the provided AWB and Phone Number."
            else:
                error = "Please provide both AWB Number and Phone Number."
        
        return request.render('keralariders_logistics.tracking_search_page', {'error': error})

    @http.route(['/track/<string:token>'], type='http', auth="public", website=False)
    def track_shipment(self, token, **kw):
        shipment = request.env['logistics.shipment'].sudo().search([('tracking_token', '=', token)], limit=1)
        if not shipment:
            return request.not_found()
            
        state_dict = dict(shipment._fields['state'].selection)
        shipment_state_str = state_dict.get(shipment.state, shipment.state)
        
        values = {
            'shipment': shipment,
            'shipment_state_str': shipment_state_str,
        }
        return request.render('keralariders_logistics.tracking_page', values)
