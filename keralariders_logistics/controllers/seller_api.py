"""Seller-facing REST API under ``/api/v1/seller``.

Authenticated with per-seller API key + secret headers (HTTPS assumed). The
handlers reuse the portal whitelist and the same ``logistics.order`` /
``logistics.shipment`` methods the seller portal uses, so wallet debit still
happens only on Request Pickup and internal ops fields cannot be injected.
"""

import json
import logging
import time

from odoo import fields, http, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from odoo.addons.keralariders_logistics.controllers.portal import LogisticsPortal

_logger = logging.getLogger(__name__)

API_PREFIX = '/api/v1/seller'
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

# JSON body keys that map onto the portal form whitelist. Anything else,
# including fulfilment / charge / hub / DE / wallet fields, is ignored.
_SHIPMENT_ALIASES = {
    'customer_name': 'shipping_to_name',
    'shipping_to_name': 'shipping_to_name',
    'customer_phone': 'shipping_to_mobile',
    'phone': 'shipping_to_mobile',
    'shipping_to_mobile': 'shipping_to_mobile',
    'customer_address': 'shipping_to_address',
    'address': 'shipping_to_address',
    'shipping_to_address': 'shipping_to_address',
    'destination_pincode': 'shipping_to_zip',
    'dest_pincode': 'shipping_to_zip',
    'pincode': 'shipping_to_zip',
    'shipping_to_zip': 'shipping_to_zip',
    'customer_email': 'shipping_to_email',
    'email': 'shipping_to_email',
    'item_description': 'item_description',
    'description': 'item_description',
    'weight_kg': 'total_weight',
    'weight': 'total_weight',
    'total_weight': 'total_weight',
    'payment_type': 'order_payment_type',
    'order_payment_type': 'order_payment_type',
    'order_value': 'total_order_value',
    'cod_amount': 'total_order_value',
    'total_order_value': 'total_order_value',
    'pickup_date': 'pickup_date',
    'length_cm': 'length_cm',
    'breadth_cm': 'breadth_cm',
    'height_cm': 'height_cm',
    'is_cylindrical': 'is_cylindrical',
    'indiapost_article_type': 'indiapost_article_type',
    'article_type': 'indiapost_article_type',
    'indiapost_pickup_slot': 'indiapost_pickup_slot',
    'indiapost_pickup_date': 'indiapost_pickup_date',
}


class SellerApiController(http.Controller):

    def _json(self, payload, status=200, extra_headers=None):
        headers = {'Content-Type': 'application/json'}
        if extra_headers:
            headers.update(extra_headers)
        body = json.dumps(payload, default=str)
        if hasattr(request, 'make_json_response'):
            return request.make_json_response(payload, headers=headers, status=status)
        return request.make_response(body, headers=list(headers.items()), status=status)

    def _rate_headers(self):
        return getattr(request, '_seller_api_rate_headers', None) or None

    def _ok(self, data, status=200, extra_headers=None):
        return self._json({'ok': True, 'data': data}, status=status, extra_headers=extra_headers)

    def _err(self, code, message, status=400, extra_headers=None):
        return self._json(
            {'ok': False, 'error': {'code': code, 'message': message}},
            status=status,
            extra_headers=extra_headers,
        )

    def _read_json(self):
        raw = request.httprequest.get_data(as_text=True) or ''
        if not raw.strip():
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            raise UserError(_("Request body must be valid JSON."))
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise UserError(_("JSON body must be an object."))
        return payload

    def _extract_credentials(self):
        headers = request.httprequest.headers
        api_key = (headers.get('X-Api-Key') or '').strip()
        api_secret = (headers.get('X-Api-Secret') or '').strip()
        auth = (headers.get('Authorization') or '').strip()
        if auth.lower().startswith('bearer '):
            token = auth[7:].strip()
            if ':' in token:
                bearer_key, bearer_secret = token.split(':', 1)
                api_key = api_key or bearer_key.strip()
                api_secret = api_secret or bearer_secret.strip()
            elif token:
                api_key = api_key or token
        return api_key, api_secret

    def _authenticate(self):
        """Return (credential, error_response). error_response is None on success."""
        api_key, api_secret = self._extract_credentials()
        prefix = (api_key[:12] + '…') if api_key else ''
        if not api_key or not api_secret:
            return None, self._err(
                'unauthorized',
                'Missing API credentials. Send X-Api-Key and X-Api-Secret.',
                status=401,
            ), prefix
        Credential = request.env['logistics.seller.api.credential'].sudo()
        found = Credential.search([('api_key', '=', api_key)], limit=1)
        if not found:
            return None, self._err(
                'unauthorized', 'Invalid API credentials.', status=401,
            ), prefix
        if found.state != 'active':
            return None, self._err(
                'unauthorized', 'API key is disabled or revoked.', status=401,
            ), prefix
        credential = Credential.authenticate(api_key, api_secret)
        if not credential:
            return None, self._err(
                'unauthorized', 'Invalid API credentials.', status=401,
            ), prefix
        allowed, limit, remaining = credential.consume_rate_limit()
        rate_headers = {
            'X-RateLimit-Limit': str(limit),
            'X-RateLimit-Remaining': str(remaining if allowed else 0),
        }
        request._seller_api_rate_headers = rate_headers
        if not allowed:
            headers = dict(rate_headers, **{'Retry-After': '3600'})
            return None, self._err(
                'rate_limited',
                'Daily API request limit reached for this key.',
                status=429,
                extra_headers=headers,
            ), prefix
        return credential, None, prefix

    def _log(self, credential, prefix, endpoint, status, error_code, started, extra_seller=None):
        seller = extra_seller or (credential.seller_id if credential else False)
        request.env['logistics.seller.api.log'].log_call({
            'seller_id': seller.id if seller else False,
            'credential_id': credential.id if credential else False,
            'endpoint': endpoint,
            'method': request.httprequest.method,
            'http_status': status,
            'remote_addr': request.httprequest.remote_addr or '',
            'duration_ms': int((time.time() - started) * 1000),
            'error_code': error_code or '',
            'api_key_prefix': prefix,
        })

    def _response_text(self, response):
        if hasattr(response, 'get_data'):
            try:
                return response.get_data(as_text=True) or ''
            except TypeError:
                data = response.get_data() or b''
                return data.decode('utf-8') if isinstance(data, bytes) else (data or '')
        data = getattr(response, 'data', '') or ''
        if isinstance(data, bytes):
            return data.decode('utf-8')
        return data

    def _dispatch(self, endpoint, handler):
        started = time.time()
        credential, error, prefix = self._authenticate()
        if error:
            status = getattr(error, 'status_code', 401)
            code = 'rate_limited' if status == 429 else 'unauthorized'
            self._log(None, prefix, endpoint, status, code, started)
            return error
        seller = credential.seller_id
        try:
            response = handler(credential, seller)
            status = getattr(response, 'status_code', 200)
            error_code = ''
            try:
                payload = json.loads(self._response_text(response) or '{}')
                if not payload.get('ok'):
                    error_code = (payload.get('error') or {}).get('code') or ''
            except Exception:
                payload = {}
            self._log(credential, prefix, endpoint, status, error_code, started, seller)
            return response
        except (UserError, ValidationError) as exc:
            message = str(exc)
            code, status = self._classify_user_error(message)
            self._log(credential, prefix, endpoint, status, code, started, seller)
            return self._err(code, message, status=status, extra_headers=self._rate_headers())
        except AccessError as exc:
            self._log(credential, prefix, endpoint, 403, 'forbidden', started, seller)
            return self._err('forbidden', str(exc), status=403)
        except Exception:
            _logger.exception('Seller API failure on %s', endpoint)
            self._log(credential, prefix, endpoint, 500, 'server_error', started, seller)
            return self._err(
                'server_error',
                'Something went wrong. Please retry, or contact KeralaXpress support.',
                status=500,
            )

    @staticmethod
    def _classify_user_error(message):
        lower = (message or '').lower()
        if 'insufficient' in lower and 'wallet' in lower:
            return 'insufficient_wallet', 402
        if 'unknown pincode' in lower or 'cannot find any hub' in lower:
            return 'not_serviceable', 400
        if 'only draft' in lower or 'not eligible' in lower or 'can only request' in lower:
            return 'not_eligible', 409
        return 'validation_error', 400

    def _portal(self):
        return LogisticsPortal()

    def _payload_to_post(self, payload):
        post = {}
        for key, value in (payload or {}).items():
            mapped = _SHIPMENT_ALIASES.get(key)
            if not mapped:
                continue
            if isinstance(value, bool):
                post[mapped] = '1' if value else ''
            elif value is None:
                continue
            else:
                post[mapped] = str(value)
        return post

    def _shipment_data(self, shipment, include_events=False):
        shipment.ensure_one()
        payment_labels = dict(shipment._fields['order_payment_type'].selection)
        state_label = shipment.get_tracking_status_label()
        data = {
            'awb': shipment.name,
            'order_reference': shipment.order_id.name or False,
            'state': shipment.state,
            'state_label': state_label,
            'customer_name': shipment.shipping_to_name or '',
            'customer_phone': shipment.shipping_to_mobile or '',
            'customer_address': shipment.shipping_to_address or '',
            'destination_pincode': shipment.shipping_to_zip or '',
            'origin_pincode': shipment.shipping_from_zip or '',
            'weight_kg': shipment.total_weight or 0.0,
            'item_description': shipment.item_description or '',
            'payment_type': shipment.order_payment_type,
            'payment_type_label': payment_labels.get(shipment.order_payment_type, ''),
            'cod_amount': shipment.cod_amount or 0.0,
            'order_value': shipment.total_order_value or 0.0,
            'delivery_charge': shipment.delivery_charges_total or 0.0,
            'currency': shipment.currency_id.name if shipment.currency_id else 'INR',
            'tracking_url': shipment.tracking_url or False,
            'tracking_token': shipment.tracking_token or False,
            'pickup_requested_on': shipment.pickup_requested_on or False,
            'is_return': bool(shipment.is_return_journey),
        }
        article = getattr(shipment, 'indiapost_article_number', False)
        if article:
            data['carrier_article_number'] = article
        if include_events:
            events = shipment.get_tracking_timeline(newest_first=False, public=True)
            data['events'] = [
                {
                    'time': entry.get('time_display') or entry.get('time') or '',
                    'label': entry.get('label') or '',
                    'detail': entry.get('detail') or '',
                }
                for entry in events
            ]
        return data

    def _idempotency_key(self):
        return (request.httprequest.headers.get('Idempotency-Key') or '').strip()

    def _replay_or_begin(self, seller, endpoint, payload):
        key = self._idempotency_key()
        if not key:
            return None, None
        Idem = request.env['logistics.seller.api.idempotency'].sudo()
        fingerprint = Idem.fingerprint(request.httprequest.method, endpoint, payload)
        existing = Idem.find_replay(seller, key)
        if existing:
            if existing.request_fingerprint != fingerprint:
                return self._err(
                    'conflict',
                    'Idempotency-Key was already used with a different request.',
                    status=409,
                ), fingerprint
            stored = json.loads(existing.response_body or '{}')
            return self._json(stored, status=existing.http_status), fingerprint
        return None, fingerprint

    def _store_idempotency(self, seller, fingerprint, status, payload, shipment=None, endpoint=None):
        key = self._idempotency_key()
        if not key or not fingerprint:
            return
        try:
            request.env['logistics.seller.api.idempotency'].store(
                seller, key, fingerprint, status, payload,
                shipment=shipment, endpoint=endpoint,
            )
        except Exception:
            _logger.warning('Seller API: could not store idempotency key', exc_info=True)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    @http.route(
        API_PREFIX + '/wallet', type='http', auth='public', methods=['GET'],
        csrf=False, save_session=False,
    )
    def seller_api_wallet(self, **kw):
        def handler(credential, seller):
            wallet = request.env['logistics.wallet'].sudo().search(
                [('seller_id', '=', seller.id)], limit=1,
            )
            currency = wallet.currency_id if wallet else request.env.company.currency_id
            return self._ok({
                'balance': wallet.balance if wallet else 0.0,
                'currency': currency.name if currency else 'INR',
                'currency_symbol': currency.symbol if currency else '₹',
            }, extra_headers=self._rate_headers())
        return self._dispatch(API_PREFIX + '/wallet', handler)

    @http.route(
        API_PREFIX + '/rates', type='http', auth='public', methods=['POST'],
        csrf=False, save_session=False,
    )
    def seller_api_rates(self, **kw):
        def handler(credential, seller):
            payload = self._read_json()
            origin = (
                payload.get('origin_pincode')
                or payload.get('origin')
                or (seller.zip or '')
            )
            dest = (
                payload.get('destination_pincode')
                or payload.get('dest_pincode')
                or payload.get('pincode')
                or ''
            )
            origin = str(origin or '').strip()
            dest = str(dest or '').strip()
            try:
                weight = float(payload.get('weight_kg') or payload.get('weight') or 0)
            except (TypeError, ValueError):
                return self._err('validation_error', 'weight_kg must be a number.')
            if weight <= 0:
                return self._err('validation_error', 'weight_kg must be greater than 0.')
            if not dest:
                return self._err('validation_error', 'destination_pincode is required.')

            portal = self._portal()
            method = portal._calculator_method(seller)
            post = {
                'origin_pincode': origin,
                'dest_pincode': dest,
                'weight': str(weight),
                'length_cm': payload.get('length_cm') or '',
                'breadth_cm': payload.get('breadth_cm') or '',
                'height_cm': payload.get('height_cm') or '',
                'indiapost_article_type': payload.get('article_type') or payload.get('indiapost_article_type') or 'SP',
                'insurance_value': payload.get('insurance_value') or 0,
            }
            if method == 'indiapost':
                try:
                    length = float(payload.get('length_cm') or 0)
                    breadth = float(payload.get('breadth_cm') or 0)
                    height = float(payload.get('height_cm') or 0)
                    insurance = float(payload.get('insurance_value') or 0)
                except (TypeError, ValueError):
                    return self._err(
                        'validation_error',
                        'Please enter the weight and all three dimensions as numbers.',
                    )
                if not (length > 0 and breadth > 0 and height > 0):
                    return self._err(
                        'validation_error',
                        'India Post prices on size as well as weight, so length_cm, '
                        'breadth_cm and height_cm are all required.',
                    )
                article = portal._ip_article_type_from_post(post)
                quote = request.env['logistics.indiapost.tariff'].sudo().quote_safe(
                    origin, dest,
                    article_type=article,
                    weight_kg=weight,
                    length_cm=length,
                    breadth_cm=breadth,
                    height_cm=height,
                    insurance_value=insurance,
                )
                if not quote.get('ok'):
                    return self._err('validation_error', quote.get('error') or 'Unable to quote.')
                return self._ok({
                    'method': 'indiapost',
                    'origin_pincode': origin,
                    'destination_pincode': dest,
                    'weight_kg': weight,
                    'charge': quote.get('total_payable'),
                    'currency': 'INR',
                    'detail': {
                        'article_type': quote.get('article_type'),
                        'base_tariff': quote.get('base_tariff'),
                        'tax': quote.get('total_tax'),
                        'total_payable': quote.get('total_payable'),
                    },
                }, extra_headers=self._rate_headers())

            District = request.env['logistics.district'].sudo()
            origin_info = District.get_district_from_pincode(origin)
            dest_info = District.get_district_from_pincode(dest)
            origin_district = origin_info.get('district_id')
            dest_district = dest_info.get('district_id')
            if not origin_district:
                return self._err(
                    'not_serviceable',
                    'Unknown origin pincode %s.' % origin,
                )
            if not dest_district:
                return self._err(
                    'not_serviceable',
                    'Unknown destination pincode %s.' % dest,
                )
            post['origin_district_id'] = str(origin_district.id)
            post['dest_district_id'] = str(dest_district.id)
            quote, error = portal._calculator_quote_slabs(post, seller)
            if error:
                return self._err('validation_error', error)
            return self._ok({
                'method': 'own_network',
                'origin_pincode': origin,
                'destination_pincode': dest,
                'origin_district': quote.get('origin_name'),
                'destination_district': quote.get('dest_name'),
                'same_district': quote.get('same_district'),
                'weight_kg': weight,
                'charge': quote.get('total_payable'),
                'currency': 'INR',
            }, extra_headers=self._rate_headers())
        return self._dispatch(API_PREFIX + '/rates', handler)

    @http.route(
        API_PREFIX + '/pincodes/<string:pincode>', type='http', auth='public',
        methods=['GET'], csrf=False, save_session=False,
    )
    def seller_api_pincode(self, pincode, **kw):
        def handler(credential, seller):
            pin = (pincode or '').strip()
            info = request.env['logistics.district'].sudo().get_district_from_pincode(pin)
            district = info.get('district_id')
            hub = request.env['logistics.hub'].sudo().browse()
            if pin:
                try:
                    hub = request.env['logistics.hub'].sudo().get_hub_from_pincode(pin)
                except UserError:
                    hub = request.env['logistics.hub'].sudo().browse()
            serviceable = bool(district and hub)
            return self._ok({
                'pincode': pin,
                'serviceable': serviceable,
                'district': district.name if district else (info.get('district_name') or False),
                'state': (district.state_id.name if district and district.state_id else info.get('state_name') or False),
                'hub': hub.name if hub else False,
            }, extra_headers=self._rate_headers())
        return self._dispatch(API_PREFIX + '/pincodes/<pincode>', handler)

    @http.route(
        API_PREFIX + '/shipments', type='http', auth='public',
        methods=['GET', 'POST'], csrf=False, save_session=False,
    )
    def seller_api_shipments(self, **kw):
        if request.httprequest.method == 'GET':
            return self._dispatch(API_PREFIX + '/shipments', self._list_shipments)
        return self._dispatch(API_PREFIX + '/shipments', self._create_shipment)

    def _list_shipments(self, credential, seller):
        args = request.httprequest.args
        try:
            page = max(1, int(args.get('page') or 1))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = int(args.get('page_size') or DEFAULT_PAGE_SIZE)
        except (TypeError, ValueError):
            page_size = DEFAULT_PAGE_SIZE
        page_size = min(max(1, page_size), MAX_PAGE_SIZE)
        domain = [('seller_id', '=', seller.id)]
        state = (args.get('state') or '').strip()
        if state:
            domain.append(('state', '=', state))
        awb = (args.get('awb') or '').strip()
        if awb:
            domain.append(('name', '=', awb))
        Shipment = request.env['logistics.shipment'].sudo()
        total = Shipment.search_count(domain)
        offset = (page - 1) * page_size
        shipments = Shipment.search(
            domain, order='create_date desc, id desc',
            limit=page_size, offset=offset,
        )
        return self._ok({
            'page': page,
            'page_size': page_size,
            'total': total,
            'shipments': [self._shipment_data(s) for s in shipments],
        }, extra_headers=self._rate_headers())

    def _create_shipment(self, credential, seller):
        payload = self._read_json()
        replay, fingerprint = self._replay_or_begin(seller, API_PREFIX + '/shipments', payload)
        if replay:
            return replay

        book = payload.get('book', True)
        if isinstance(book, str):
            book = book.strip().lower() not in ('0', 'false', 'no')

        post = self._payload_to_post(payload)
        pickup_date = (post.get('pickup_date') or '').strip() or fields.Date.context_today(
            request.env['logistics.order'].sudo()
        ).isoformat()
        post['pickup_date'] = pickup_date
        post.setdefault('indiapost_pickup_date', pickup_date)

        portal = self._portal()
        shipment = request.env['logistics.shipment'].sudo().browse()
        with request.env.cr.savepoint():
            vals = portal._portal_draft_shipment_vals(
                seller, post, require_known_pincode=True,
            )
            order = request.env['logistics.order'].sudo().create({
                'seller_id': seller.id,
                'pickup_date': pickup_date,
            })
            vals['order_id'] = order.id
            # Never copy charge / fulfilment / custody keys from the payload.
            for banned in (
                'fulfilment_method', 'delivery_charges_subtotal',
                'delivery_charges_total', 'tax_percentage',
                'wallet_transaction_id', 'custodian_type', 'current_hub_id',
                'pickup_executive_id', 'delivery_executive_id', 'state',
            ):
                vals.pop(banned, None)
            shipment = request.env['logistics.shipment'].sudo().create(vals)
            if shipment.order_payment_type == 'cod':
                if payload.get('cod_amount') not in (None, ''):
                    try:
                        shipment.cod_amount = float(payload.get('cod_amount'))
                    except (TypeError, ValueError):
                        shipment.cod_amount = shipment.total_order_value
                else:
                    shipment.cod_amount = shipment.total_order_value
            portal._shipment_quote_after_create(shipment)
            if book:
                order.sudo().action_request_pickup()

        data = self._shipment_data(shipment, include_events=True)
        data['booked'] = bool(book)
        data['wallet_debited'] = bool(book and shipment.wallet_transaction_id)
        if book and shipment.wallet_transaction_id:
            data['wallet_debit'] = abs(shipment.wallet_transaction_id.amount)
        body = {'ok': True, 'data': data}
        self._store_idempotency(
            seller, fingerprint, 201, body, shipment=shipment,
            endpoint=API_PREFIX + '/shipments',
        )
        return self._json(body, status=201, extra_headers=self._rate_headers())

    @http.route(
        API_PREFIX + '/shipments/<string:awb>', type='http', auth='public',
        methods=['GET'], csrf=False, save_session=False,
    )
    def seller_api_shipment_get(self, awb, **kw):
        def handler(credential, seller):
            shipment = self._find_seller_shipment(seller, awb)
            if not shipment:
                return self._err('not_found', 'Shipment not found.', status=404)
            return self._ok(
                self._shipment_data(shipment, include_events=True),
                extra_headers=self._rate_headers(),
            )
        return self._dispatch(API_PREFIX + '/shipments/<awb>', handler)

    @http.route(
        API_PREFIX + '/shipments/<string:awb>/track', type='http', auth='public',
        methods=['GET'], csrf=False, save_session=False,
    )
    def seller_api_shipment_track(self, awb, **kw):
        def handler(credential, seller):
            shipment = self._find_seller_shipment(seller, awb)
            if not shipment:
                return self._err('not_found', 'Shipment not found.', status=404)
            events = shipment.get_tracking_timeline(newest_first=False, public=True)
            return self._ok({
                'awb': shipment.name,
                'state': shipment.state,
                'state_label': shipment.get_tracking_status_label(),
                'origin': shipment.get_tracking_origin_label(),
                'destination': shipment.get_tracking_destination_label(),
                'tracking_url': shipment.tracking_url or False,
                'events': [
                    {
                        'time': entry.get('time_display') or entry.get('time') or '',
                        'label': entry.get('label') or '',
                        'detail': entry.get('detail') or '',
                    }
                    for entry in events
                ],
            }, extra_headers=self._rate_headers())
        return self._dispatch(API_PREFIX + '/shipments/<awb>/track', handler)

    @http.route(
        API_PREFIX + '/shipments/<string:awb>/pickup', type='http', auth='public',
        methods=['POST'], csrf=False, save_session=False,
    )
    def seller_api_shipment_pickup(self, awb, **kw):
        def handler(credential, seller):
            shipment = self._find_seller_shipment(seller, awb)
            if not shipment:
                return self._err('not_found', 'Shipment not found.', status=404)
            order = shipment.order_id
            if not order:
                return self._err(
                    'not_eligible',
                    'This shipment has no order, so pickup cannot be requested.',
                    status=409,
                )
            if order.state != 'draft':
                return self._err(
                    'not_eligible',
                    'Pickup can only be requested while the order is still a draft.',
                    status=409,
                )
            order.sudo().action_request_pickup()
            return self._ok({
                **self._shipment_data(shipment, include_events=True),
                'booked': True,
                'wallet_debited': bool(shipment.wallet_transaction_id),
                'wallet_debit': abs(shipment.wallet_transaction_id.amount) if shipment.wallet_transaction_id else 0.0,
            }, extra_headers=self._rate_headers())
        return self._dispatch(API_PREFIX + '/shipments/<awb>/pickup', handler)

    @http.route(
        API_PREFIX + '/shipments/<string:awb>/return', type='http', auth='public',
        methods=['POST'], csrf=False, save_session=False,
    )
    def seller_api_shipment_return(self, awb, **kw):
        def handler(credential, seller):
            shipment = request.env['logistics.shipment'].sudo().search([
                ('seller_id', '=', seller.id),
                ('name', '=', (awb or '').strip()),
                '|',
                ('state', '=', 'delivered'),
                '&', '&', '&',
                ('state', '=', 'delivery_failed'),
                ('custodian_type', '=', 'hub'),
                ('delivery_fail_count', '>=', 1),
                ('is_return_journey', '=', False),
            ], limit=1)
            if not shipment:
                return self._err(
                    'not_eligible',
                    'Shipment not found or not eligible for return.',
                    status=409,
                )
            shipment.sudo().action_request_return()
            return self._ok({
                **self._shipment_data(shipment, include_events=True),
                'wallet_debited': False,
            }, extra_headers=self._rate_headers())
        return self._dispatch(API_PREFIX + '/shipments/<awb>/return', handler)

    def _find_seller_shipment(self, seller, awb):
        name = (awb or '').strip()
        if not name:
            return request.env['logistics.shipment'].sudo().browse()
        Shipment = request.env['logistics.shipment'].sudo()
        shipment = Shipment.search([
            ('seller_id', '=', seller.id),
            ('name', '=', name),
        ], limit=1)
        if shipment:
            return shipment
        # Allow tracking_token lookup for the owning seller only.
        return Shipment.search([
            ('seller_id', '=', seller.id),
            ('tracking_token', '=', name),
        ], limit=1)
