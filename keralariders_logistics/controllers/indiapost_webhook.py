"""Inbound India Post event webhooks.

India Post's Customer Self-Service Portal is configured in Webhook mode and
POSTs to the two paths registered there. There is no published authentication
scheme, so the endpoints stay public and the HTTP response is always a generic
OK — never shipment data. Unknown articles are logged and acknowledged with 200
so India Post does not retry-storm.

GET and empty POST are treated as pings (the portal Test buttons).
"""

from odoo import http
from odoo.http import request

import json
import logging

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc

_logger = logging.getLogger(__name__)

_BODY_LOG_LIMIT = 4000


class IndiapostWebhook(http.Controller):

    @http.route(ipc.BOOKING_WEBHOOK_PATH, type='http', auth='public',
                methods=['GET', 'POST'], csrf=False, save_session=False)
    def booking_event_webhook(self, **kw):
        return self._handle('booking', ipc.BOOKING_WEBHOOK_PATH, **kw)

    @http.route(ipc.OTHER_WEBHOOK_PATH, type='http', auth='public',
                methods=['GET', 'POST'], csrf=False, save_session=False)
    def other_event_webhook(self, **kw):
        return self._handle('other', ipc.OTHER_WEBHOOK_PATH, **kw)

    def _ok(self, status=200):
        return request.make_response(
            json.dumps({'status': 'ok'}),
            headers=[('Content-Type', 'application/json')],
            status=status,
        )

    def _bad(self):
        return request.make_response(
            json.dumps({'status': 'error'}),
            headers=[('Content-Type', 'application/json')],
            status=400,
        )

    def _handle(self, kind, endpoint, **kw):
        remote = request.httprequest.remote_addr or ''
        raw = request.httprequest.get_data(as_text=True) or ''
        _logger.info(
            'India Post %s webhook (%d bytes from %s)',
            kind, len(raw), remote,
        )
        payload, error = self._read_payload(raw, **kw)
        if error == 'unparseable':
            self._log(endpoint, kind, remote, raw, payload=None,
                      http_status=400, success=False,
                      error_message='Unparseable webhook body')
            return self._bad()

        try:
            request.env['logistics.indiapost.tracking'].sudo().ingest_webhook(
                kind, payload)
        except Exception:
            _logger.exception(
                'India Post %s webhook failed after parse; acknowledging 200',
                kind,
            )
            self._log(endpoint, kind, remote, raw, payload=payload,
                      http_status=200, success=False,
                      error_message='Webhook handler error; see server log')
            return self._ok()

        self._log(endpoint, kind, remote, raw, payload=payload,
                  http_status=200, success=True)
        return self._ok()

    def _read_payload(self, raw, **kw):
        """Return ``(payload, error)``. ``error`` is ``unparseable`` or None."""
        httprequest = request.httprequest
        if httprequest.method == 'GET':
            return {}, None

        content_type = (httprequest.content_type or '').lower()
        stripped = (raw or '').strip()
        form = {
            key: value for key, value in kw.items()
            if key != 'csrf_token'
        }

        if not stripped:
            return form, None

        looks_json = 'json' in content_type or stripped[:1] in '{['
        if looks_json:
            try:
                payload = json.loads(stripped)
            except (ValueError, TypeError):
                return None, 'unparseable'
            return payload, None

        if form:
            return form, None
        return {}, None

    def _log(self, endpoint, kind, remote, raw, payload, http_status,
             success, error_message=None):
        body = payload if payload is not None else raw[:_BODY_LOG_LIMIT]
        keys = list(payload.keys()) if isinstance(payload, dict) else None
        request.env['logistics.indiapost.log'].sudo().log_call({
            'endpoint': endpoint,
            'method': request.httprequest.method,
            'operation': 'webhook-%s' % kind,
            'http_status': http_status,
            'success': success,
            'request_body': {
                'ip': remote,
                'keys': keys,
                'body': body,
            },
            'response_body': {'status': 'ok' if http_status < 400 else 'error'},
            'error_message': error_message,
        })
