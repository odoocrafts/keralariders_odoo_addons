from odoo import http
from odoo.http import request

class TrackingController(http.Controller):

    @http.route(['/track/<string:token>'], type='http', auth="public", website=False)
    def track_shipment(self, token, **kw):
        shipment = request.env['logistics.shipment'].sudo().search([('tracking_token', '=', token)], limit=1)
        if not shipment:
            return request.not_found()
            
        values = {
            'shipment': shipment,
        }
        return request.render('keralariders_logistics.tracking_page', values)
