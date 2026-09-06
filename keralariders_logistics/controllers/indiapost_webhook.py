"""Placeholder for the India Post inbound status webhook.

DELIBERATELY DISABLED. The vendor document mentions an inbound webhook, but it
specifies no authentication scheme, no source IP range and no retry policy, so
there is no safe way to implement it: an unauthenticated public endpoint that
mutates shipment states would be a straightforward way to forge deliveries.

The route below exists so the URL can be handed to India Post for
whitelisting and so the shape of the eventual implementation is obvious. It
accepts nothing and changes nothing; it records the payload in the API log and
returns 501.

When India Post publishes the contract, the work is:
  1. verify the caller (shared secret header, HMAC signature or mTLS);
  2. normalise the payload into the same ``tracking_details`` shape the bulk
     tracking endpoint returns;
  3. hand it to ``logistics.indiapost.tracking._ip_apply_tracking``.
Step 3 already exists and is what the polling cron uses, so no rework is
needed in the status mapping itself.
"""

from odoo import http
from odoo.http import request

import json
import logging

_logger = logging.getLogger(__name__)

# Flip this only once an authentication scheme has been agreed and implemented
# in _authenticate below.
WEBHOOK_ENABLED = False


class IndiapostWebhook(http.Controller):

    @http.route('/indiapost/webhook/status', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def indiapost_status_webhook(self, **kw):
        """Accept nothing, change nothing, and say so."""
        raw = request.httprequest.get_data(as_text=True) or ''
        _logger.info(
            'India Post webhook called but the endpoint is disabled '
            '(%d bytes from %s)',
            len(raw), request.httprequest.remote_addr,
        )
        request.env['logistics.indiapost.log'].sudo().log_call({
            'endpoint': '/indiapost/webhook/status',
            'method': 'POST',
            'operation': 'webhook (disabled)',
            'http_status': 501,
            'success': False,
            'request_body': raw[:5000],
            'error_message': 'Inbound webhook is disabled: India Post has not '
                             'published an authentication scheme.',
        })
        return request.make_response(
            json.dumps({
                'success': False,
                'error': 'This endpoint is not enabled. KeralaXpress polls the '
                         'bulk tracking API instead.',
            }),
            headers=[('Content-Type', 'application/json')],
            status=501,
        )
