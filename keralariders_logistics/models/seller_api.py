"""Seller REST API credentials, idempotency keys, and request audit log.

Secrets are stored as a salted HMAC-SHA256 and are never written back to the
database in plaintext. The plaintext secret is returned once from
``generate_for_seller`` so the portal can flash it; after that only the hash
remains.
"""

import hashlib
import hmac
import json
import logging
import secrets
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

KEY_PREFIX = 'kx_'
SECRET_PREFIX = 'kxs_'
DEFAULT_DAILY_LIMIT = 5000
IDEMPOTENCY_HOURS = 48
LOG_RETENTION_DAYS = 90
PBKDF_LIKE_HMAC = hashlib.sha256


def _new_public_key():
    return KEY_PREFIX + secrets.token_urlsafe(24)


def _new_secret():
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def _new_salt():
    return secrets.token_hex(16)


def _hash_secret(secret, salt):
    """Fast keyed hash. Secrets are high-entropy random tokens, not passwords."""
    return hmac.new(
        salt.encode('utf-8'),
        (secret or '').encode('utf-8'),
        PBKDF_LIKE_HMAC,
    ).hexdigest()


def _looks_like_secret(value):
    text = (value or '').strip()
    return text.startswith(SECRET_PREFIX) or len(text) > 40


class SellerApiCredential(models.Model):
    _name = 'logistics.seller.api.credential'
    _description = 'Seller API Credential'
    _order = 'create_date desc, id desc'

    seller_id = fields.Many2one(
        'logistics.seller', string='Seller', required=True,
        ondelete='cascade', index=True,
    )
    name = fields.Char(string='Label', default='Live key')
    api_key = fields.Char(
        string='API Key', required=True, index=True, copy=False, readonly=True,
    )
    secret_salt = fields.Char(
        required=True, copy=False,
        groups='keralariders_logistics.group_logistics_admin',
    )
    secret_hash = fields.Char(
        required=True, copy=False,
        groups='keralariders_logistics.group_logistics_admin',
    )
    state = fields.Selection(
        [
            ('active', 'Active'),
            ('disabled', 'Disabled'),
            ('revoked', 'Revoked'),
        ],
        string='Status', default='active', required=True, index=True,
    )
    last_used_at = fields.Datetime(string='Last Used', readonly=True)
    request_count = fields.Integer(string='Requests Today', default=0, readonly=True)
    request_count_date = fields.Date(string='Count Date', readonly=True)
    note = fields.Char(string='Note')

    _sql_constraints = [
        ('api_key_uniq', 'unique(api_key)', 'API keys must be unique.'),
    ]

    @api.model
    def _daily_limit(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(
            'keralariders_logistics.seller_api_daily_limit',
            str(DEFAULT_DAILY_LIMIT),
        )
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            limit = DEFAULT_DAILY_LIMIT
        return max(1, limit)

    @api.model
    def generate_for_seller(self, seller, name=None):
        """Create an active key for ``seller``. Returns (record, plaintext secret).

        Any previously active keys for the seller are revoked first, so a
        regenerate is the same path as a first generate.
        """
        if not seller:
            raise UserError(_("A seller is required to generate API credentials."))
        existing = self.sudo().search([
            ('seller_id', '=', seller.id),
            ('state', '=', 'active'),
        ])
        if existing:
            existing.write({'state': 'revoked'})
        plaintext = _new_secret()
        salt = _new_salt()
        credential = self.sudo().create({
            'seller_id': seller.id,
            'name': name or _('Live key'),
            'api_key': _new_public_key(),
            'secret_salt': salt,
            'secret_hash': _hash_secret(plaintext, salt),
            'state': 'active',
        })
        return credential, plaintext

    def action_disable(self):
        for rec in self:
            if rec.state == 'active':
                rec.state = 'disabled'
        return True

    def action_revoke(self):
        self.write({'state': 'revoked'})
        return True

    def _secret_matches(self, secret):
        self.ensure_one()
        hashed = self.sudo()
        if not secret or not hashed.secret_hash or not hashed.secret_salt:
            return False
        candidate = _hash_secret(secret, hashed.secret_salt)
        return hmac.compare_digest(candidate, hashed.secret_hash)

    @api.model
    def authenticate(self, api_key, api_secret):
        """Return the active credential for this key+secret, or an empty recordset."""
        key = (api_key or '').strip()
        secret = (api_secret or '').strip()
        if not key or not secret:
            return self.browse()
        credential = self.sudo().search([('api_key', '=', key)], limit=1)
        if not credential or credential.state != 'active':
            return self.browse()
        if not credential._secret_matches(secret):
            return self.browse()
        return credential

    def consume_rate_limit(self):
        """Increment today's counter. Returns (allowed, limit, remaining)."""
        self.ensure_one()
        today = fields.Date.context_today(self)
        limit = self._daily_limit()
        count = self.request_count or 0
        if self.request_count_date != today:
            count = 0
        if count >= limit:
            return False, limit, 0
        count += 1
        self.sudo().write({
            'request_count': count,
            'request_count_date': today,
            'last_used_at': fields.Datetime.now(),
        })
        return True, limit, max(0, limit - count)


class SellerApiIdempotency(models.Model):
    _name = 'logistics.seller.api.idempotency'
    _description = 'Seller API Idempotency Key'
    _order = 'create_date desc, id desc'

    seller_id = fields.Many2one(
        'logistics.seller', string='Seller', required=True,
        ondelete='cascade', index=True,
    )
    idempotency_key = fields.Char(required=True, index=True)
    request_fingerprint = fields.Char(required=True)
    http_status = fields.Integer(required=True)
    response_body = fields.Text(required=True)
    shipment_id = fields.Many2one(
        'logistics.shipment', string='Shipment', ondelete='set null',
    )
    endpoint = fields.Char()

    _sql_constraints = [
        (
            'seller_key_uniq',
            'unique(seller_id, idempotency_key)',
            'This idempotency key was already used.',
        ),
    ]

    @api.model
    def fingerprint(self, method, path, body):
        raw = json.dumps(body or {}, sort_keys=True, default=str, separators=(',', ':'))
        payload = '%s\n%s\n%s' % ((method or '').upper(), path or '', raw)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    @api.model
    def find_replay(self, seller, key):
        if not seller or not key:
            return self.browse()
        cutoff = fields.Datetime.now() - timedelta(hours=IDEMPOTENCY_HOURS)
        return self.sudo().search([
            ('seller_id', '=', seller.id),
            ('idempotency_key', '=', key.strip()),
            ('create_date', '>=', cutoff),
        ], limit=1)

    @api.model
    def store(self, seller, key, fingerprint, status, body, shipment=None, endpoint=None):
        self.gc_for_seller(seller)
        return self.sudo().create({
            'seller_id': seller.id,
            'idempotency_key': key.strip(),
            'request_fingerprint': fingerprint,
            'http_status': status,
            'response_body': json.dumps(body, default=str),
            'shipment_id': shipment.id if shipment else False,
            'endpoint': endpoint,
        })

    @api.model
    def gc_for_seller(self, seller):
        if not seller:
            return 0
        cutoff = fields.Datetime.now() - timedelta(hours=IDEMPOTENCY_HOURS)
        stale = self.sudo().search([
            ('seller_id', '=', seller.id),
            ('create_date', '<', cutoff),
        ])
        count = len(stale)
        stale.unlink()
        return count

    @api.model
    def gc_stale(self):
        cutoff = fields.Datetime.now() - timedelta(hours=IDEMPOTENCY_HOURS)
        stale = self.sudo().search([('create_date', '<', cutoff)])
        count = len(stale)
        stale.unlink()
        return count


class SellerApiLog(models.Model):
    _name = 'logistics.seller.api.log'
    _description = 'Seller API Request Log'
    _order = 'create_date desc, id desc'
    _rec_name = 'endpoint'

    seller_id = fields.Many2one(
        'logistics.seller', string='Seller', ondelete='set null', index=True,
    )
    credential_id = fields.Many2one(
        'logistics.seller.api.credential', string='Credential',
        ondelete='set null', index=True,
    )
    endpoint = fields.Char(required=True, index=True)
    method = fields.Char()
    http_status = fields.Integer(index=True)
    remote_addr = fields.Char(string='IP')
    duration_ms = fields.Integer(string='Duration (ms)')
    error_code = fields.Char(index=True)
    api_key_prefix = fields.Char(string='Key prefix')

    @api.model
    def log_call(self, vals):
        """Persist one call. Never store secrets or request bodies."""
        clean = {
            'seller_id': vals.get('seller_id') or False,
            'credential_id': vals.get('credential_id') or False,
            'endpoint': (vals.get('endpoint') or '')[:256],
            'method': (vals.get('method') or '')[:16],
            'http_status': vals.get('http_status') or 0,
            'remote_addr': (vals.get('remote_addr') or '')[:64],
            'duration_ms': vals.get('duration_ms') or 0,
            'error_code': (vals.get('error_code') or '')[:64],
            'api_key_prefix': (vals.get('api_key_prefix') or '')[:16],
        }
        if _looks_like_secret(clean['api_key_prefix']):
            clean['api_key_prefix'] = ''
        try:
            self.sudo().create(clean)
        except Exception:  # pragma: no cover - logging must never break a call
            _logger.exception(
                'Seller API: could not persist log for %s %s',
                clean.get('method'), clean.get('endpoint'),
            )

    @api.model
    def gc_logs(self, days=LOG_RETENTION_DAYS):
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        stale = self.sudo().search([('create_date', '<', cutoff)])
        count = len(stale)
        stale.unlink()
        return count

    @api.model
    def _cron_gc(self):
        logs = self.gc_logs()
        keys = self.env['logistics.seller.api.idempotency'].gc_stale()
        _logger.info(
            'Seller API housekeeping: dropped %s log rows, %s idempotency keys',
            logs, keys,
        )


class Seller(models.Model):
    _inherit = 'logistics.seller'

    api_credential_ids = fields.One2many(
        'logistics.seller.api.credential', 'seller_id', string='API Credentials',
    )
    api_log_ids = fields.One2many(
        'logistics.seller.api.log', 'seller_id', string='API Logs',
    )

    def action_generate_api_credentials(self):
        """Admin helper: mint a new key. The secret is not shown in the backend."""
        self.ensure_one()
        if not self.env.user.has_group('keralariders_logistics.group_logistics_admin'):
            raise AccessError(_("Only a Logistics Administrator can generate keys here."))
        credential, _secret = self.env['logistics.seller.api.credential'].generate_for_seller(self)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('API key created'),
                'message': _(
                    'Key %s was created. The secret is only shown in the '
                    'seller portal — ask the seller to generate the key there.'
                ) % credential.api_key,
                'type': 'warning',
                'sticky': True,
            },
        }
