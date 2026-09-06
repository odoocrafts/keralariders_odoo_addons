"""Transport layer for the India Post (Department of Posts) bulk customer API.

Everything that talks HTTP goes through :meth:`IndiapostClient.call`, which owns
timeouts, retries, credential redaction, logging and the translation of API
errors into Odoo user-facing errors. No other model should import ``requests``.
"""

from odoo import api, fields, models, _
from odoo.exceptions import UserError

import json
import logging
import threading
import time

import requests

from . import indiapost_common as ipc

_logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = 'https://test.cept.gov.in/beextcustomer'
LOGIN_PATH = '/v1/access/login'

# Tokens are Keycloak JWTs valid for 900 seconds. Renew with a minute to spare
# so a long booking batch cannot expire mid-flight.
TOKEN_REFRESH_MARGIN_S = 60
TOKEN_FALLBACK_TTL_S = 900

CONNECT_TIMEOUT_S = 10
DEFAULT_READ_TIMEOUT_S = 60

MAX_ATTEMPTS = 3
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRY_BACKOFF_S = (0.5, 1.5)

CONFIG_PREFIX = 'keralariders_logistics.'

# Settings and their defaults. Read through _ip_settings so every consumer sees
# the same normalised dictionary.
SETTING_DEFAULTS = {
    'indiapost_enabled': False,
    'indiapost_environment': 'sandbox',
    'indiapost_base_url': DEFAULT_BASE_URL,
    'indiapost_username': '',
    'indiapost_password': '',
    'indiapost_customer_id': '',
    'indiapost_sp_contract_id': '',
    'indiapost_bp_contract_id': '',
    'indiapost_sender_name': '',
    'indiapost_sender_company': '',
    'indiapost_sender_address': '',
    'indiapost_sender_city': '',
    'indiapost_sender_state': '',
    'indiapost_sender_pincode': '',
    'indiapost_sender_mobile': '',
    'indiapost_sender_email': '',
    'indiapost_sender_gstin': '',
    'indiapost_booking_office_name': '',
    'indiapost_booking_office_pin': '',
    'indiapost_label_size': 'A6',
    'indiapost_transmission_mode': 'S',
    'indiapost_tariff_cache_minutes': '30',
    'indiapost_office_cache_days': '30',
    'indiapost_request_timeout': str(DEFAULT_READ_TIMEOUT_S),
    'indiapost_pickup_lead_days': '1',
    'indiapost_default_pod': False,
    'indiapost_quote_markup_percent': '0',
}


class IndiapostApiError(Exception):
    """An India Post call failed at the transport or envelope level.

    ``field_errors`` carries the per-field list the label endpoint returns on
    HTTP 422 so callers can render something better than a blob of JSON.
    """

    def __init__(self, message, status=None, payload=None, field_errors=None,
                 endpoint=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload
        self.field_errors = field_errors or []
        self.endpoint = endpoint

    def user_message(self):
        parts = [self.message]
        for entry in self.field_errors:
            if isinstance(entry, dict):
                parts.append('  • %s: %s' % (
                    entry.get('field') or _('field'),
                    entry.get('message') or entry.get('value') or '',
                ))
            else:
                parts.append('  • %s' % entry)
        if self.status:
            parts.append(_('(India Post returned HTTP %s)') % self.status)
        return '\n'.join(parts)


class _TokenCache:
    """Per-process access token cache.

    Deliberately not stored in ir.config_parameter: a bearer token is a
    credential and that table is readable by more users than it should be. With
    900 second tokens each Odoo worker logs in a handful of times an hour,
    which is well inside any sane rate limit.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return None
            token, expires_at = entry
            if time.time() >= expires_at - TOKEN_REFRESH_MARGIN_S:
                return None
            return token

    def set(self, key, token, expires_in):
        try:
            ttl = int(expires_in or 0)
        except (TypeError, ValueError):
            ttl = 0
        ttl = ttl if ttl > 0 else TOKEN_FALLBACK_TTL_S
        with self._lock:
            self._entries[key] = (token, time.time() + ttl)

    def clear(self, key=None):
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)


_TOKENS = _TokenCache()


class IndiapostClient(models.AbstractModel):
    _name = 'logistics.indiapost.client'
    _description = 'India Post API Client'

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    @api.model
    def _ip_settings(self):
        """All India Post settings as a plain dict, with defaults applied."""
        params = self.env['ir.config_parameter'].sudo()
        settings = {}
        for key, default in SETTING_DEFAULTS.items():
            value = params.get_param(CONFIG_PREFIX + key)
            if value in (None, False, ''):
                value = default
            settings[key] = value
        # Booleans arrive as the strings config parameters store.
        for key in ('indiapost_enabled', 'indiapost_default_pod'):
            settings[key] = str(settings[key]).strip().lower() in (
                '1', 'true', 't', 'yes',
            )
        settings['indiapost_base_url'] = (
            str(settings['indiapost_base_url']).strip().rstrip('/')
            or DEFAULT_BASE_URL
        )
        for key, fallback in (
            ('indiapost_tariff_cache_minutes', 30),
            ('indiapost_office_cache_days', 30),
            ('indiapost_request_timeout', DEFAULT_READ_TIMEOUT_S),
            ('indiapost_pickup_lead_days', 1),
        ):
            try:
                settings[key] = max(int(float(settings[key])), 0)
            except (TypeError, ValueError):
                settings[key] = fallback
        try:
            settings['indiapost_quote_markup_percent'] = max(
                float(settings['indiapost_quote_markup_percent']), 0.0)
        except (TypeError, ValueError):
            settings['indiapost_quote_markup_percent'] = 0.0
        return settings

    @api.model
    def _ip_is_configured(self):
        """True when there are enough credentials to attempt a call."""
        settings = self._ip_settings()
        return bool(
            settings['indiapost_enabled']
            and settings['indiapost_username']
            and settings['indiapost_password']
        )

    @api.model
    def _ip_require_configured(self):
        settings = self._ip_settings()
        if not settings['indiapost_enabled']:
            raise UserError(_(
                'The India Post integration is switched off. Enable it under '
                'Settings > Logistics > India Post.'
            ))
        if not (settings['indiapost_username'] and settings['indiapost_password']):
            raise UserError(_(
                'India Post credentials are missing. Set the bulk customer '
                'username and password under Settings > Logistics > India Post.'
            ))
        return settings

    @api.model
    def _ip_contract_id(self, settings, article_type):
        """The contract id to book one product against.

        India Post contracts a bulk customer per service, so the Speed Post
        and Business Parcel contracts are two different numbers rather than
        two uses of one. Resolving it here, by product, is what keeps a
        Business Parcel from being booked against the Speed Post contract.

        A missing contract is refused by name: sent empty, India Post answers
        "Article at index 0 is missing bulk_customer_id or contract_id", which
        never says which of the two it wanted.
        """
        key = ipc.CONTRACT_SETTING_BY_ARTICLE_TYPE.get(article_type)
        if not key:
            raise UserError(_(
                '%s is not an India Post product. Articles can only be booked '
                'as Speed Post (SP) or Business Parcel (BP).'
            ) % (article_type or _('(none)')))
        product = ipc.ARTICLE_TYPE_LABELS.get(article_type, article_type)
        contract_id = str(settings.get(key) or '').strip()
        if not contract_id:
            raise UserError(_(
                'No India Post %(product)s contract id is configured, so a '
                '%(product)s article cannot be booked. Add the %(product)s '
                'contract under Settings > Logistics > India Post.'
            ) % {'product': product})
        if not ipc.CONTRACT_ID_RE.match(contract_id):
            raise UserError(_(
                'The India Post %(product)s contract id must be exactly 8 '
                'digits, but it is set to "%(value)s". Correct it under '
                'Settings > Logistics > India Post.'
            ) % {'product': product, 'value': contract_id})
        return contract_id

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    @api.model
    def _ip_token_key(self, settings):
        return (settings['indiapost_base_url'], settings['indiapost_username'])

    @api.model
    def _ip_login(self, settings):
        """Exchange the credentials for a bearer token and cache it."""
        response = self._ip_request(
            'POST', LOGIN_PATH,
            settings=settings,
            body={
                'username': settings['indiapost_username'],
                'password': settings['indiapost_password'],
            },
            token=None,
            operation='login',
        )
        data = (response.payload or {}).get('data') or {}
        token = data.get('access_token')
        if not token:
            raise IndiapostApiError(
                _('India Post rejected the configured credentials.'),
                status=response.status,
                payload=response.payload,
                endpoint=LOGIN_PATH,
            )
        _TOKENS.set(self._ip_token_key(settings), token, data.get('expires_in'))
        # The response also carries a refresh_token valid for 1800 s, but no
        # refresh endpoint is specified anywhere we can verify, so we simply
        # log in again when the access token nears expiry.
        return token

    @api.model
    def _ip_token(self, settings, force_refresh=False):
        key = self._ip_token_key(settings)
        if force_refresh:
            _TOKENS.clear(key)
        else:
            token = _TOKENS.get(key)
            if token:
                return token
        return self._ip_login(settings)

    @api.model
    def _ip_forget_token(self):
        _TOKENS.clear()

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------
    @api.model
    def call(self, method, path, params=None, body=None, operation=None,
             shipment=None, order=None, expect_pdf=False, settings=None):
        """Make an authenticated API call.

        ``path`` is appended to the configured base URL. Note that the bulk
        booking endpoint is ``/process-articles/{id}`` with no ``/v1`` prefix,
        unlike everything else.
        """
        settings = settings or self._ip_require_configured()
        token = self._ip_token(settings)
        try:
            return self._ip_request(
                method, path, settings=settings, params=params, body=body,
                token=token, operation=operation, shipment=shipment,
                order=order, expect_pdf=expect_pdf,
            )
        except IndiapostApiError as error:
            if error.status != 401:
                raise
            # The token was rejected, most likely because it expired between
            # our margin check and the call landing. Get a fresh one once.
            _logger.info('India Post: token rejected, re-authenticating')
            token = self._ip_token(settings, force_refresh=True)
            return self._ip_request(
                method, path, settings=settings, params=params, body=body,
                token=token, operation=operation, shipment=shipment,
                order=order, expect_pdf=expect_pdf,
            )

    @api.model
    def _ip_request(self, method, path, settings, params=None, body=None,
                    token=None, operation=None, shipment=None, order=None,
                    expect_pdf=False):
        url = settings['indiapost_base_url'] + path
        headers = {'Accept': 'application/pdf, application/json' if expect_pdf
                   else 'application/json'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if token:
            headers['Authorization'] = 'Bearer %s' % token

        timeout = (CONNECT_TIMEOUT_S, settings['indiapost_request_timeout']
                   or DEFAULT_READ_TIMEOUT_S)
        started = time.time()
        last_error = None
        response = None
        attempt = 0

        while attempt < MAX_ATTEMPTS:
            attempt += 1
            try:
                response = requests.request(
                    method, url, params=params,
                    data=json.dumps(body) if body is not None else None,
                    headers=headers, timeout=timeout,
                )
            except requests.exceptions.RequestException as exc:
                last_error = exc
                response = None
                if attempt < MAX_ATTEMPTS:
                    time.sleep(RETRY_BACKOFF_S[min(attempt - 1,
                                                   len(RETRY_BACKOFF_S) - 1)])
                    continue
                break
            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                last_error = None
                time.sleep(RETRY_BACKOFF_S[min(attempt - 1,
                                               len(RETRY_BACKOFF_S) - 1)])
                continue
            break

        duration_ms = int((time.time() - started) * 1000)
        log_vals = {
            'endpoint': path,
            'method': method,
            'operation': operation or path,
            'environment': settings['indiapost_environment'],
            'duration_ms': duration_ms,
            'attempts': attempt,
            'request_body': body if body is not None else params,
            'shipment_id': shipment.id if shipment else False,
            'order_id': order.id if order else False,
        }
        Log = self.env['logistics.indiapost.log']

        if response is None:
            message = _(
                'Could not reach India Post at %(url)s after %(attempts)s '
                'attempts. The server may not be whitelisted, or the API may '
                'be down.'
            ) % {'url': settings['indiapost_base_url'], 'attempts': attempt}
            Log.log_call(dict(
                log_vals, success=False, http_status=0,
                error_message='%s: %r' % (message, last_error),
            ))
            raise IndiapostApiError(message, status=0, endpoint=path)

        content = response.content or b''
        content_type = (response.headers.get('Content-Type') or '').lower()
        is_pdf = content[:4] == b'%PDF' or 'application/pdf' in content_type
        payload = None
        if not is_pdf:
            try:
                payload = response.json() if content else None
            except ValueError:
                payload = None

        parsed = _IndiapostResponse(
            status=response.status_code,
            payload=payload,
            content=content,
            headers=dict(response.headers),
            is_pdf=is_pdf,
            attempts=attempt,
        )

        error = self._ip_extract_error(parsed, path)
        Log.log_call(dict(
            log_vals,
            http_status=parsed.status,
            success=not error,
            response_body=content if is_pdf else (payload if payload is not None
                                                  else content),
            error_message=error.message if error else '',
            correlation_id=(payload or {}).get('correlation_id')
            if isinstance(payload, dict) else '',
        ))
        if error:
            raise error
        return parsed

    @api.model
    def _ip_extract_error(self, response, path):
        """Turn a failed response into an IndiapostApiError, or None if fine.

        Careful: per-article booking failures come back as HTTP 200 with
        ``success: true`` and populated ``error_articles``. Those are *not*
        errors at this layer; the booking model inspects them.
        """
        payload = response.payload
        if response.is_pdf:
            return None if response.status in (200, 201) else IndiapostApiError(
                _('India Post returned HTTP %s for the label request.')
                % response.status,
                status=response.status, endpoint=path,
            )

        if 200 <= response.status < 300:
            if isinstance(payload, dict) and payload.get('success') is False:
                return IndiapostApiError(
                    self._ip_error_text(payload),
                    status=response.status, payload=payload,
                    field_errors=self._ip_field_errors(payload), endpoint=path,
                )
            return None

        if response.status == 401:
            return IndiapostApiError(
                _('India Post rejected the access token.'),
                status=401, payload=payload, endpoint=path,
            )
        return IndiapostApiError(
            self._ip_error_text(payload) or (
                _('India Post returned HTTP %s.') % response.status),
            status=response.status, payload=payload,
            field_errors=self._ip_field_errors(payload), endpoint=path,
        )

    @staticmethod
    def _ip_error_text(payload):
        """Best human-readable sentence out of the several error shapes seen."""
        if not isinstance(payload, dict):
            return ''
        error = payload.get('error')
        if isinstance(error, dict):
            return str(error.get('message') or payload.get('message') or '')
        candidates = [error, payload.get('message'), payload.get('detail')]
        for candidate in candidates:
            if candidate and isinstance(candidate, str):
                return candidate
        return ''

    @staticmethod
    def _ip_field_errors(payload):
        """The label endpoint reports HTTP 422 as error.field_errors[]."""
        if not isinstance(payload, dict):
            return []
        error = payload.get('error')
        if isinstance(error, dict) and isinstance(error.get('field_errors'), list):
            return error['field_errors']
        if isinstance(payload.get('field_errors'), list):
            return payload['field_errors']
        return []

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    @api.model
    def action_ip_test_connection(self):
        """Settings button: log in and resolve one pincode."""
        settings = self._ip_require_configured()
        try:
            self._ip_token(settings, force_refresh=True)
            pincode = ipc.normalize_pincode(
                settings['indiapost_sender_pincode'] or '682001',
                _('Consignor pincode'),
            )
            offices = self.env['logistics.indiapost.office'].sudo()._ip_fetch_offices(
                pincode, settings=settings)
        except ipc.IndiapostDataError as exc:
            raise UserError(str(exc)) from exc
        except IndiapostApiError as exc:
            raise UserError(exc.user_message()) from exc

        bookable = [office for office in offices if ipc.office_is_bookable(office)]
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('India Post connection OK'),
                'message': _(
                    'Authenticated against %(env)s. Pincode %(pin)s resolved '
                    '%(total)s offices, %(bookable)s bookable.'
                ) % {
                    'env': settings['indiapost_environment'],
                    'pin': pincode,
                    'total': len(offices),
                    'bookable': len(bookable),
                },
                'type': 'success',
                'sticky': False,
            },
        }


class _IndiapostResponse:
    """Small value object so callers do not depend on ``requests``."""

    __slots__ = ('status', 'payload', 'content', 'headers', 'is_pdf', 'attempts')

    def __init__(self, status, payload, content, headers, is_pdf, attempts):
        self.status = status
        self.payload = payload
        self.content = content
        self.headers = headers
        self.is_pdf = is_pdf
        self.attempts = attempts

    @property
    def data(self):
        """The ``data`` member of the standard envelope, or the payload itself."""
        if isinstance(self.payload, dict) and 'data' in self.payload:
            return self.payload['data']
        return self.payload
