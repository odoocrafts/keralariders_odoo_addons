from odoo import http
from odoo.http import request

_NOT_FOUND = (
    "No shipment found with the provided AWB or India Post article number."
)
_EMPTY = "Please provide an AWB or India Post article number."


def _article_search_values(query):
    """Exact article values to try, covering typical case variants."""
    query = (query or '').strip()
    if not query:
        return []
    values = [query, query.upper(), query.lower()]
    seen = set()
    unique = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


class TrackingController(http.Controller):

    def _find_public_shipment(self, query):
        """Resolve a public tracking query to one ``logistics.shipment``.

        Accepts KeralaXpress AWB (``name``), the public UUID token, or an
        India Post article number. Public callers already sudo this model
        for AWB lookup; article search uses the same access.
        """
        query = (query or '').strip()
        if not query:
            return request.env['logistics.shipment'].sudo().browse()

        Shipment = request.env['logistics.shipment'].sudo()

        by_name = Shipment.search([('name', '=', query)], limit=1)
        if by_name:
            return by_name

        by_token = Shipment.search([('tracking_token', '=', query)], limit=1)
        if by_token:
            return by_token

        article_vals = _article_search_values(query)
        matches = Shipment.search([
            ('indiapost_article_number', 'in', article_vals),
        ])
        if not matches:
            # Case-insensitive exact match for mixed-case stored values, or
            # when the query merely *looks* like XX########XIN.
            matches = Shipment.search([
                ('indiapost_article_number', '=ilike', query),
            ])
        if not matches:
            return Shipment.browse()
        if len(matches) == 1:
            return matches

        preferred = matches.filtered(
            lambda s: s.fulfilment_method == 'indiapost') or matches
        booked = preferred.filtered('indiapost_booked_on')
        pool = booked or preferred

        def _latest_key(shipment):
            stamped = shipment.indiapost_booked_on or shipment.create_date
            stamp = stamped.timestamp() if stamped else 0
            return (stamp, shipment.id)

        return pool.sorted(key=_latest_key, reverse=True)[:1]

    def _redirect_to_canonical_track(self, shipment):
        token = (shipment.tracking_token or '').strip()
        if token:
            return request.redirect(f'/track/{token}')
        return None

    @http.route(['/track'], type='http', auth="public", website=False, methods=['GET', 'POST'], csrf=False)
    def track_search(self, **kwargs):
        error = None
        query = None

        if request.httprequest.method == 'POST':
            query = (kwargs.get('awb') or kwargs.get('id') or '').strip()
        elif request.httprequest.method == 'GET' and kwargs.get('id'):
            query = kwargs.get('id', '').strip()

        if query:
            shipment = self._find_public_shipment(query)
            if shipment:
                redirect = self._redirect_to_canonical_track(shipment)
                if redirect:
                    return redirect
            error = _NOT_FOUND
        elif request.httprequest.method == 'POST':
            error = _EMPTY
        values = {
            'error': error,
            'query': query or '',
            'company': request.env.company,
        }
        return request.render('keralariders_logistics.tracking_search_page', values)

    @http.route(['/track/<string:token>'], type='http', auth="public", website=False)
    def track_shipment(self, token, **kw):
        shipment = request.env['logistics.shipment'].sudo().search(
            [('tracking_token', '=', token)], limit=1)
        if not shipment:
            shipment = self._find_public_shipment(token)
            if shipment:
                redirect = self._redirect_to_canonical_track(shipment)
                if redirect:
                    return redirect
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

        shipment_state_str = shipment.get_tracking_status_label()
        payment_labels = dict(shipment._fields['order_payment_type'].selection)
        delivery_display = shipment.get_tracking_delivery_display()
        tracking_events = shipment.get_tracking_timeline(newest_first=False, public=True)
        progress_steps = shipment.get_tracking_progress_steps()
        weight_display = False
        if shipment.total_weight:
            weight_display = f"{shipment.total_weight:.3f} kg"

        # Final-mile contact: show DE mobile only for OFD / delivered (not earlier stages).
        delivery_contact_phone = False
        if shipment.state in ('out_for_delivery', 'delivered') and shipment.delivery_executive_id:
            delivery_contact_phone = (shipment.delivery_executive_id.mobile or '').strip() or False

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
            'delivery_contact_phone': delivery_contact_phone,
            'company': request.env.company,
            'languages': [],
        }
        return request.render('keralariders_logistics.tracking_page', values)
