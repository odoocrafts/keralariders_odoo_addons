from odoo import api, fields, models, _

import json
import logging
import re

_logger = logging.getLogger(__name__)

# Anything whose key matches this never reaches the database. The password and
# the bearer token in particular would otherwise sit in a table any logistics
# administrator can read.
_SECRET_KEY_RE = re.compile(
    r'password|passwd|secret|token|authorization|credential|api[_-]?key',
    re.IGNORECASE,
)
_REDACTED = '***redacted***'
_BODY_MAX_LEN = 20000


def redact(value):
    """Deep-copy a payload with every credential-looking value masked."""
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _SECRET_KEY_RE.search(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def to_text(value, limit=_BODY_MAX_LEN):
    """Render a payload as readable, truncated, redacted text for the log."""
    if value in (None, '', False):
        return ''
    if isinstance(value, bytes):
        if value[:4] == b'%PDF':
            return '<application/pdf, %d bytes>' % len(value)
        value = value.decode('utf-8', 'replace')
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value[:limit]
    try:
        text = json.dumps(redact(value), indent=2, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        return text[:limit] + '\n... truncated ...'
    return text


class IndiapostLog(models.Model):
    _name = 'logistics.indiapost.log'
    _description = 'India Post API Call Log'
    _order = 'create_date desc, id desc'
    _rec_name = 'endpoint'

    endpoint = fields.Char(string='Endpoint', required=True, index=True)
    method = fields.Char(string='HTTP Method')
    operation = fields.Char(
        string='Operation', index=True,
        help='Which part of the integration made the call, e.g. booking or tariff.',
    )
    environment = fields.Selection(
        [('sandbox', 'Sandbox'), ('production', 'Production')],
        string='Environment',
    )
    http_status = fields.Integer(string='HTTP Status', index=True)
    success = fields.Boolean(string='Succeeded', index=True)
    duration_ms = fields.Integer(string='Duration (ms)')
    attempts = fields.Integer(string='Attempts', default=1)
    request_body = fields.Text(string='Request (redacted)')
    response_body = fields.Text(string='Response (redacted)')
    error_message = fields.Text(string='Error')
    shipment_id = fields.Many2one(
        'logistics.shipment', string='Shipment', ondelete='set null', index=True,
    )
    order_id = fields.Many2one(
        'logistics.order', string='Order', ondelete='set null',
    )
    correlation_id = fields.Char(string='Correlation Id', index=True)

    @api.model
    def log_call(self, vals):
        """Persist one API call without deadlocking the booking transaction.

        Logs are committed on a separate cursor so a later raise still leaves a
        diagnostic row. That INSERT must not include shipment/order foreign
        keys: pickup holds those rows ``FOR UPDATE``, and a second transaction
        waiting on the FK is what hung ``/my/orders/request_pickup`` until the
        120s thread limit. FKs are attached afterwards on the caller's cursor
        (same transaction as the locks, so it does not wait).
        """
        vals = dict(vals)
        vals['request_body'] = to_text(vals.get('request_body'))
        vals['response_body'] = to_text(vals.get('response_body'))
        shipment_id = vals.pop('shipment_id', False) or False
        order_id = vals.pop('order_id', False) or False
        log_id = None
        try:
            with self.env.registry.cursor() as cr:
                log = self.with_env(self.env(cr=cr)).sudo().create(vals)
                log_id = log.id
        except Exception:  # pragma: no cover - logging must never break a call
            _logger.exception(
                'India Post: could not persist API log for %s %s',
                vals.get('method'), vals.get('endpoint'),
            )
            return
        if not log_id or not (shipment_id or order_id):
            return
        try:
            with self.env.cr.savepoint():
                self.browse(log_id).sudo().write({
                    'shipment_id': shipment_id,
                    'order_id': order_id,
                })
        except Exception:
            _logger.debug(
                'India Post: could not attach log %s to shipment/order',
                log_id, exc_info=True,
            )

    @api.model
    def gc_logs(self, days=90):
        """Drop logs older than ``days``; called from the housekeeping cron."""
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        stale = self.sudo().search([('create_date', '<', cutoff)])
        count = len(stale)
        stale.unlink()
        return count
