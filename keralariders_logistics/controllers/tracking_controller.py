from odoo import http
from odoo.http import request

class TrackingController(http.Controller):

    @http.route(['/track'], type='http', auth="public", website=False, methods=['GET', 'POST'], csrf=False)
    def track_search(self, **kwargs):
        error = None
        awb = None
        
        if request.httprequest.method == 'POST':
            awb = kwargs.get('awb', '').strip()
        elif request.httprequest.method == 'GET' and kwargs.get('id'):
            awb = kwargs.get('id', '').strip()
            
        if awb:
            shipment = request.env['logistics.shipment'].sudo().search([
                ('name', '=', awb)
            ], limit=1)
            if shipment:
                return request.redirect(f'/track/{shipment.tracking_token}')
            else:
                error = "No shipment found with the provided AWB Number."
        elif request.httprequest.method == 'POST':
            error = "Please provide an AWB Number."
        values = {
            'error': error,
            'company': request.env.company,
        }
        return request.render('keralariders_logistics.tracking_search_page', values)

    @http.route(['/track/<string:token>'], type='http', auth="public", website=False)
    def track_shipment(self, token, **kw):
        shipment = request.env['logistics.shipment'].sudo().search([('tracking_token', '=', token)], limit=1)
        if not shipment:
            return request.not_found()
            
        # If logged in as the assigned delivery executive, redirect to the portal delivery update page
        if not request.env.user._is_public():
            delivery_executive = request.env['logistics.delivery.executive'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
            if delivery_executive and shipment.delivery_executive_id.id == delivery_executive.id:
                return request.redirect(f'/my/delivery/{shipment.id}')
            
            
        state_dict = dict(shipment._fields['state'].selection)
        shipment_state_str = state_dict.get(shipment.state, shipment.state)
        
        values = {
            'shipment': shipment,
            'shipment_state_str': shipment_state_str,
            'languages': [],
        }
        return request.render('keralariders_logistics.tracking_page', values)
