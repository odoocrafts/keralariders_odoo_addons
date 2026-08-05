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

        # If logged in as a DE: claim UI when eligible, else portal delivery detail
        if not request.env.user._is_public():
            delivery_executive = request.env['logistics.delivery.executive'].sudo().search(
                [('user_id', '=', request.env.user.id)], limit=1
            )
            if delivery_executive:
                if shipment.can_de_self_assign(delivery_executive):
                    return request.redirect(f'/my/delivery/{shipment.id}/claim')
                return request.redirect(f'/my/delivery/{shipment.id}?view=1')

        state_dict = dict(shipment._fields['state'].selection)
        shipment_state_str = state_dict.get(shipment.state, shipment.state)
        payment_labels = dict(shipment._fields['order_payment_type'].selection)
        delivery_display = shipment.get_tracking_delivery_display()
        tracking_events = shipment.get_tracking_timeline(newest_first=False)
        progress_steps = shipment.get_tracking_progress_steps()
        weight_display = False
        if shipment.total_weight:
            weight_display = f"{shipment.total_weight:.3f} kg"

        values = {
            'shipment': shipment,
            'shipment_state_str': shipment_state_str,
            'tracking_events': tracking_events,
            'progress_steps': progress_steps,
            'delivery_display': delivery_display,
            'origin_label': shipment.get_tracking_origin_label(),
            'destination_label': shipment.get_tracking_destination_label(),
            'payment_type_label': payment_labels.get(shipment.order_payment_type, ''),
            'weight_display': weight_display,
            'last_update_display': shipment._format_tracking_datetime(shipment.write_date),
            'company': request.env.company,
            'languages': [],
        }
        return request.render('keralariders_logistics.tracking_page', values)
