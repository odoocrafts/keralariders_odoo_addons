"""India Post tariff lookups, with a short-lived cache.

The public rate calculator is ``auth="public"``, so without a cache an
anonymous visitor could turn the page into a load generator against India
Post. Quotes are therefore cached on
(environment, article type, source pincode, destination pincode, weight band,
dimensions, VAS flags) for a configurable number of minutes.

Speed Post is priced on ``/v1/speed-post/tariffs`` with
``product-code=SP_INLAND_PARCEL`` (from 500 g inclusive; lighter Speed Post
selections are redirected to Business Parcel). Business Parcel is a
different table: production answers on ``/v1/business-parcel-tariff/calculate``
with ``product-code=BP``, and the Speed Post path still returns HTTP 422
"No matching domestic speed post tariff found … product: BUSINESS_PARCEL".

Lakshadweep PINs sit in the 682 Kerala series, so a Kerala origin is priced
as within-state (``distance_km`` ``WS``). The counter charges the Zone/Metro
slab (``ZM``). The tariff GET has no zone parameter — ``zone``,
``distance-band``, ``metro`` and ``distance-type`` are ignored — so a
within-state Lakshadweep quote is replaced with the tariff of a destination
the API itself classifies as ``ZM``. The article is still booked to the real
Lakshadweep PIN.
"""

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

import hashlib
import json
import logging

from . import indiapost_common as ipc
from .indiapost_client import IndiapostApiError

_logger = logging.getLogger(__name__)

SPEED_POST_TARIFF_PATH = '/v1/speed-post/tariffs'
BUSINESS_PARCEL_TARIFF_PATH = '/v1/business-parcel-tariff/calculate'
# Historical alias: Speed Post was the only product this module quoted.
TARIFF_PATH = SPEED_POST_TARIFF_PATH

# Confirmed on 2026-10-01 against /v1/pincode-search: each of these returns
# state_name Lakshadweep (Amini, Kavaratti, Minicoy and the other islands).
# 682550 and 682560 return no offices. 682001 is Kochi, Kerala.
LAKSHADWEEP_PINCODES = frozenset(str(pin) for pin in range(682551, 682560))
LAKSHADWEEP_STATE_NAMES = frozenset({'lakshadweep', 'lakshadweep islands'})
# Cache-key marker so a within-state quote for these PINs cannot be reused
# after the Zone/Metro substitution.
LAKSHADWEEP_ZONE_CODE = 'LD'
WITHIN_STATE_ZONE = 'WS'
ZONE_METRO = 'ZM'
# Speed Post distance_km values that mean Zone/Metro. Business Parcel returns
# distance_km 0 for every lane, so its zone is read from a Speed Post call.
# Probe order: from Kerala, 600001 / 560001 / 500001 come back ZM (and at
# 2950 g that ZM slab is base 190, tax 34, final 224). 400001 and 110001 come
# back OS (other states, final 296) and are only reached when an earlier probe
# is not ZM for that origin. These PINs are never booked.
ZONE_METRO_PROBE_PINCODES = (
    '600001', '560001', '500001', '400001', '110001', '700001',
)
# Lightest Speed Post parcel the tariff API accepts, used only to read
# distance_km. The zone does not depend on weight.
ZONE_CHECK_WEIGHT_G = 500
ZONE_CHECK_LENGTH_CM = 14
ZONE_CHECK_BREADTH_CM = 9
ZONE_CHECK_HEIGHT_CM = 1

# Value added services we can price. INS carries a declared value, the rest are
# flags. Observed on a 250 g Kochi -> Delhi article with a base tariff of 77:
# POD 10, REG 5, ACK 10, OTP 1.5, insurance 58 on 1,000 declared and 2,998 on
# 50,000 declared. Insurance is steeply non-linear and can dwarf the postage,
# which is why the calculator shows it as a separate line.
VAS_FLAGS = ('pod', 'reg', 'ack', 'otp')


class IndiapostTariffCache(models.Model):
    _name = 'logistics.indiapost.tariff.cache'
    _description = 'India Post Tariff Cache'
    _order = 'create_date desc'
    _rec_name = 'cache_key'

    cache_key = fields.Char(string='Cache Key', required=True, index=True)
    source_pincode = fields.Char(string='From Pincode', index=True)
    destination_pincode = fields.Char(string='To Pincode', index=True)
    weight_g = fields.Integer(string='Weight (g)')
    length_cm = fields.Integer(string='Length (cm)')
    breadth_cm = fields.Integer(string='Breadth (cm)')
    height_cm = fields.Integer(string='Height (cm)')
    vas_key = fields.Char(string='VAS')
    product_code = fields.Char(string='Product')
    chargeable_weight_g = fields.Integer(string='Chargeable Weight (g)')
    is_local = fields.Boolean(string='Local')
    distance_display = fields.Char(
        string='Distance',
        help='As returned by the API. Usually kilometres, but the literal '
             'string "OS" (out of station) also occurs.',
    )
    base_tariff = fields.Float(string='Base Tariff')
    vas_charges = fields.Float(string='VAS Charges')
    cgst = fields.Float(string='CGST')
    sgst = fields.Float(string='SGST')
    total_tax = fields.Float(string='Total Tax')
    final_amount = fields.Float(string='Total')
    delivery_type = fields.Char(string='Delivery Type')
    payload = fields.Text(string='Raw Response')
    expires_at = fields.Datetime(string='Expires At', required=True, index=True)

    _sql_constraints = [
        ('cache_key_uniq', 'UNIQUE (cache_key)', 'Duplicate tariff cache key.'),
    ]

    @api.model
    def gc_expired(self):
        """Drop expired entries; called from the housekeeping cron."""
        expired = self.sudo().search(
            [('expires_at', '<', fields.Datetime.now())])
        count = len(expired)
        expired.unlink()
        return count

    def action_ip_invalidate(self):
        self.unlink()
        return True


class IndiapostTariff(models.AbstractModel):
    _name = 'logistics.indiapost.tariff'
    _description = 'India Post Tariff Service'

    # ------------------------------------------------------------------
    # Request assembly
    # ------------------------------------------------------------------
    @api.model
    def _ip_vas_key(self, insurance_value=0.0, **flags):
        parts = ['%s=%s' % (flag, int(bool(flags.get(flag))))
                 for flag in VAS_FLAGS]
        parts.append('ins=%.2f' % (float(insurance_value or 0.0)))
        return ','.join(parts)

    @api.model
    def _ip_cache_key(self, source_pincode, destination_pincode, weight_g,
                      length_cm, breadth_cm, height_cm, vas_key, environment,
                      article_type=ipc.ARTICLE_TYPE_SPEED_POST, zone_code=''):
        parts = [
            environment, article_type,
            self._ip_tariff_product_code(article_type),
            source_pincode, destination_pincode,
            weight_g, length_cm, breadth_cm, height_cm, vas_key,
        ]
        # Only Lakshadweep quotes carry a marker, so every other cached rate
        # keeps the key it had before Zone/Metro substitution.
        if zone_code:
            parts.append(zone_code)
        raw = '|'.join(str(part) for part in parts)
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    @api.model
    def _ip_is_lakshadweep_pincode(self, pincode):
        """True when this destination is Lakshadweep, not Kerala.

        The island PINs 682551–682559 were each confirmed with pincode-search
        ``state_name`` Lakshadweep. Any other PIN is Lakshadweep when a cached
        office already says so, which is how a PIN outside that range is
        picked up without a hardcoded list of one.
        """
        digits = ''.join(ch for ch in str(pincode or '') if ch.isdigit())
        if digits in LAKSHADWEEP_PINCODES:
            return True
        if len(digits) != 6:
            return False
        office = self.env['logistics.indiapost.office'].sudo().search([
            ('pincode', '=', digits),
            ('state_name', '!=', False),
        ], limit=1)
        state = (office.state_name or '').strip().lower()
        return state in LAKSHADWEEP_STATE_NAMES

    @api.model
    def _ip_distance_zone(self, payload):
        """Speed Post zone code from ``distance_km`` (``WS``, ``ZM``, ``OS``)."""
        _numeric, display = ipc.as_distance((payload or {}).get('distance_km'))
        return (display or '').strip().upper()

    @api.model
    def _ip_fetch_tariff_payload(self, source_pincode, destination_pincode,
                                 weight_g, length_cm, breadth_cm, height_cm,
                                 insurance_value, article_type, settings,
                                 shipment, **flags):
        response = self.env['logistics.indiapost.client'].call(
            'GET', self._ip_tariff_path(article_type),
            params=self._ip_tariff_params(
                source_pincode, destination_pincode, weight_g, length_cm,
                breadth_cm, height_cm, insurance_value=insurance_value,
                article_type=article_type, **flags),
            operation='tariff', shipment=shipment, settings=settings,
        )
        return self._ip_normalize_tariff_payload(response.payload or {})

    @api.model
    def _ip_as_metro_payload(self, payload, destination_pincode, probe_pincode):
        """Keep the Zone/Metro money and the real destination PIN."""
        payload = dict(payload)
        payload['destination_pincode'] = destination_pincode
        payload['distance_km'] = ZONE_METRO
        payload['zone_probe_pincode'] = probe_pincode
        return payload

    @api.model
    def _ip_speed_post_zone(self, source_pincode, destination_pincode,
                            settings, shipment):
        """Zone code for a PIN pair. Business Parcel responses omit it."""
        try:
            payload = self._ip_fetch_tariff_payload(
                source_pincode, destination_pincode, ZONE_CHECK_WEIGHT_G,
                ZONE_CHECK_LENGTH_CM, ZONE_CHECK_BREADTH_CM,
                ZONE_CHECK_HEIGHT_CM, 0.0, ipc.ARTICLE_TYPE_SPEED_POST,
                settings, shipment,
            )
        except IndiapostApiError as exc:
            _logger.warning(
                'India Post zone check %s -> %s failed: %s',
                source_pincode, destination_pincode, exc.message,
            )
            return ''
        return self._ip_distance_zone(payload)

    @api.model
    def _ip_zone_metro_speed_post_payload(self, source_pincode,
                                          destination_pincode, weight_g,
                                          length_cm, breadth_cm, height_cm,
                                          insurance_value, article_type,
                                          settings, shipment, **flags):
        """First probe PIN whose Speed Post tariff is Zone/Metro, or None."""
        for probe in ZONE_METRO_PROBE_PINCODES:
            if probe in (source_pincode, destination_pincode):
                continue
            try:
                payload = self._ip_fetch_tariff_payload(
                    source_pincode, probe, weight_g, length_cm, breadth_cm,
                    height_cm, insurance_value, article_type, settings,
                    shipment, **flags,
                )
            except IndiapostApiError as exc:
                _logger.info(
                    'Zone/metro probe %s failed: %s', probe, exc.message)
                continue
            if self._ip_distance_zone(payload) != ZONE_METRO:
                continue
            return self._ip_as_metro_payload(
                payload, destination_pincode, probe)
        return None

    @api.model
    def _ip_zone_metro_business_parcel_payload(self, source_pincode,
                                               destination_pincode, weight_g,
                                               length_cm, breadth_cm, height_cm,
                                               insurance_value, settings,
                                               shipment, **flags):
        """Business Parcel amount for a probe PIN that Speed Post calls ZM."""
        for probe in ZONE_METRO_PROBE_PINCODES:
            if probe in (source_pincode, destination_pincode):
                continue
            if self._ip_speed_post_zone(
                    source_pincode, probe, settings, shipment) != ZONE_METRO:
                continue
            try:
                payload = self._ip_fetch_tariff_payload(
                    source_pincode, probe, weight_g, length_cm, breadth_cm,
                    height_cm, insurance_value,
                    ipc.ARTICLE_TYPE_BUSINESS_PARCEL, settings, shipment,
                    **flags,
                )
            except IndiapostApiError as exc:
                _logger.info(
                    'Business Parcel zone/metro probe %s failed: %s',
                    probe, exc.message)
                continue
            return self._ip_as_metro_payload(
                payload, destination_pincode, probe)
        return None

    @api.model
    def _ip_priced_payload(self, source_pincode, destination_pincode, weight_g,
                           length_cm, breadth_cm, height_cm, insurance_value,
                           article_type, settings, shipment, lakshadweep=False,
                           **flags):
        """Tariff body to cache. Lakshadweep within-state becomes Zone/Metro.

        A Kerala origin to 682552 is ``WS`` only because that series is the
        Kerala circle. Delhi to the same PIN is already ``OS``, and that
        other-states figure is kept: the substitution replaces within-state,
        it does not pull every lane down to the metro slab.
        """
        if not lakshadweep:
            return self._ip_fetch_tariff_payload(
                source_pincode, destination_pincode, weight_g, length_cm,
                breadth_cm, height_cm, insurance_value, article_type,
                settings, shipment, **flags)

        if article_type == ipc.ARTICLE_TYPE_BUSINESS_PARCEL:
            zone = self._ip_speed_post_zone(
                source_pincode, destination_pincode, settings, shipment)
            if zone == WITHIN_STATE_ZONE:
                metro = self._ip_zone_metro_business_parcel_payload(
                    source_pincode, destination_pincode, weight_g, length_cm,
                    breadth_cm, height_cm, insurance_value, settings, shipment,
                    **flags,
                )
                if metro:
                    return metro
            return self._ip_fetch_tariff_payload(
                source_pincode, destination_pincode, weight_g, length_cm,
                breadth_cm, height_cm, insurance_value, article_type,
                settings, shipment, **flags)

        payload = self._ip_fetch_tariff_payload(
            source_pincode, destination_pincode, weight_g, length_cm,
            breadth_cm, height_cm, insurance_value, article_type,
            settings, shipment, **flags)
        if self._ip_distance_zone(payload) != WITHIN_STATE_ZONE:
            return payload
        metro = self._ip_zone_metro_speed_post_payload(
            source_pincode, destination_pincode, weight_g, length_cm,
            breadth_cm, height_cm, insurance_value, article_type, settings,
            shipment, **flags,
        )
        return metro or payload

    @api.model
    def _ip_tariff_path(self, article_type=ipc.ARTICLE_TYPE_SPEED_POST):
        """The production document gives each product its own tariff URL."""
        if article_type == ipc.ARTICLE_TYPE_BUSINESS_PARCEL:
            return BUSINESS_PARCEL_TARIFF_PATH
        return SPEED_POST_TARIFF_PATH

    @api.model
    def _ip_tariff_product_code(self, article_type=ipc.ARTICLE_TYPE_SPEED_POST):
        """``product-code`` query value for the tariff GET.

        Speed Post (500 g and above after any light-weight redirect): request
        inland parcel so India Post does not auto-classify as
        ``SP_INLAND_DOC``. Booking still sends ``article_type=SP`` with
        ``shape_of_article=NROL``.

        Business Parcel (including Speed Post redirected below 500 g) stays
        ``BP`` on ``/v1/business-parcel-tariff/calculate``.
        """
        if article_type == ipc.ARTICLE_TYPE_BUSINESS_PARCEL:
            return ipc.ARTICLE_TYPE_BUSINESS_PARCEL
        return ipc.PRODUCT_PARCEL

    @api.model
    def _ip_normalize_tariff_payload(self, payload):
        """Speed Post and Business Parcel return the same figures in different shapes.

        Speed Post: ``vas_charges`` is a number and ``vas_details`` is a dict.
        Business Parcel: ``vas_charges`` is the dict of lines and there is no
        ``vas_details``. The rest of the quote path expects the Speed Post
        shape, so Business Parcel responses are rewritten here rather than
        forked at every consumer.
        """
        payload = dict(payload or {})
        vas = payload.get('vas_charges')
        details = payload.get('vas_details')
        if isinstance(vas, dict):
            details = details if isinstance(details, dict) else vas
            payload['vas_details'] = details
            payload['vas_charges'] = round(sum(
                ipc.as_amount(value) for value in details.values()), 2)
        elif not isinstance(details, dict):
            payload['vas_details'] = {}
        return payload

    @api.model
    def _ip_tariff_params(self, source_pincode, destination_pincode, weight_g,
                          length_cm, breadth_cm, height_cm,
                          insurance_value=0.0,
                          article_type=ipc.ARTICLE_TYPE_SPEED_POST, **flags):
        params = {
            'product-code': self._ip_tariff_product_code(article_type),
            'weight': int(weight_g),
            'source-pincode': source_pincode,
            'destination-pincode': destination_pincode,
            'length': int(length_cm),
            'width': int(breadth_cm),
            'height': int(height_cm),
        }
        if insurance_value:
            params['INS'] = int(round(float(insurance_value)))
        if flags.get('pod'):
            params['POD'] = 'YES'
        if flags.get('reg'):
            params['REG'] = 'TRUE'
        if flags.get('ack'):
            params['ACK'] = 'TRUE'
        if flags.get('otp'):
            params['OTP'] = 'TRUE'
        return params

    # ------------------------------------------------------------------
    # Quoting
    # ------------------------------------------------------------------
    @api.model
    def quote(self, source_pincode, destination_pincode, weight_kg=None,
              weight_g=None, length_cm=0, breadth_cm=0, height_cm=0,
              insurance_value=0.0, band=True, use_cache=True, shipment=None,
              settings=None, article_type=ipc.ARTICLE_TYPE_SPEED_POST,
              **flags):
        """Price one article. Raises on anything that makes a quote impossible.

        ``band`` rounds the weight up to the next 50 g postal step, which is how
        the postal slabs work anyway. It makes the cache useful and can never
        under-quote a seller. Booking sends the exact weight instead.
        """
        settings = settings or self.env['logistics.indiapost.client']._ip_require_configured()
        source_pincode = ipc.normalize_pincode(source_pincode, _('Origin pincode'))
        destination_pincode = ipc.normalize_pincode(
            destination_pincode, _('Destination pincode'))

        actual_g = int(weight_g) if weight_g else ipc.kg_to_grams(weight_kg)
        if actual_g < 1:
            raise ValidationError(_('Enter a weight greater than zero.'))
        # Light Speed Post (< 500 g) becomes Business Parcel here so calculator,
        # portal create, bulk, seller API and booking all share one rule.
        article_type = ipc.resolve_article_type(article_type, actual_g)
        length = ipc.cm_to_int(length_cm)
        breadth = ipc.cm_to_int(breadth_cm)
        height = ipc.cm_to_int(height_cm)

        errors, warnings = ipc.validate_package(actual_g, length, breadth, height)
        if errors:
            raise ValidationError('\n'.join(errors))

        billed_g = ipc.band_weight(actual_g) if band else actual_g
        vas_key = self._ip_vas_key(insurance_value=insurance_value, **flags)
        lakshadweep = self._ip_is_lakshadweep_pincode(destination_pincode)
        zone_code = LAKSHADWEEP_ZONE_CODE if lakshadweep else ''
        cache_key = self._ip_cache_key(
            source_pincode, destination_pincode, billed_g, length, breadth,
            height, vas_key, settings['indiapost_environment'],
            article_type=article_type, zone_code=zone_code,
        )

        Cache = self.env['logistics.indiapost.tariff.cache'].sudo()
        entry = None
        if use_cache:
            entry = Cache.search([
                ('cache_key', '=', cache_key),
                ('expires_at', '>', fields.Datetime.now()),
            ], limit=1)

        cached = bool(entry)
        if not entry:
            try:
                payload = self._ip_priced_payload(
                    source_pincode, destination_pincode, billed_g, length,
                    breadth, height, insurance_value, article_type, settings,
                    shipment, lakshadweep=lakshadweep, **flags,
                )
            except IndiapostApiError as exc:
                self._ip_raise_quote_api_error(
                    exc, source_pincode, destination_pincode)
            entry = self._ip_store_quote(
                cache_key, source_pincode, destination_pincode, billed_g,
                length, breadth, height, vas_key, payload, settings,
            )

        quote = self._ip_quote_from_cache(entry)
        # The India Post figures stay untouched in the cache; any KeralaXpress
        # margin is applied on top here so the passthrough stays auditable.
        markup_percent = settings['indiapost_quote_markup_percent']
        markup_amount = round(quote['final_amount'] * markup_percent / 100.0, 2)
        quote.update({
            'ok': True,
            'cached': cached,
            'article_type': article_type,
            'article_type_label': ipc.ARTICLE_TYPE_LABELS.get(
                article_type, article_type),
            'actual_weight_g': actual_g,
            'billed_weight_g': billed_g,
            'weight_banded': billed_g != actual_g,
            'volumetric_weight_g': ipc.volumetric_weight_g(length, breadth, height),
            'length_cm': length,
            'breadth_cm': breadth,
            'height_cm': height,
            'insurance_value': float(insurance_value or 0.0),
            'warnings': warnings,
            'markup_percent': markup_percent,
            'markup_amount': markup_amount,
            'total_payable': round(quote['final_amount'] + markup_amount, 2),
        })
        return quote

    @api.model
    def _ip_raise_quote_api_error(self, exc, source_pincode, destination_pincode):
        """Turn India Post tariff failures into seller-safe errors when we can.

        Unknown destination / origin PINs become a plain :class:`UserError` so
        the portal flashes a pink warning and backend Get Quote shows a dialog
        instead of an ``IndiapostApiError`` RPC traceback. Other API failures
        re-raise unchanged for callers that soft-fail or wrap them.
        """
        message = exc.message or str(exc)
        if ipc.is_pincode_not_found_message(message):
            lower = message.lower()
            fallback = destination_pincode
            if 'origin' in lower or 'source' in lower:
                fallback = source_pincode
            pin = ipc.extract_pincode_from_message(message, fallback)
            raise UserError(
                _('%s is not a valid delivery pincode.') % pin
            ) from exc
        raise exc

    @api.model
    def quote_safe(self, *args, **kwargs):
        """Like :meth:`quote` but never raises.

        Used by the public calculator, where a traceback or a 500 in front of an
        anonymous visitor is not acceptable.
        """
        try:
            return self.quote(*args, **kwargs)
        except ipc.IndiapostDataError as exc:
            return {'ok': False, 'error': str(exc), 'blocking': True}
        except (ValidationError, UserError) as exc:
            message = exc.args[0] if exc.args else str(exc)
            return {'ok': False, 'error': message, 'blocking': True}
        except IndiapostApiError as exc:
            _logger.warning('India Post tariff lookup failed: %s', exc.message)
            if ipc.is_pincode_not_found_message(exc.message):
                pin = ipc.extract_pincode_from_message(
                    exc.message, kwargs.get('destination_pincode') or (
                        args[1] if len(args) > 1 else ''))
                return {
                    'ok': False,
                    'error': _('%s is not a valid delivery pincode.') % pin,
                    'blocking': True,
                }
            # HTTP 422 means the article itself is not carriable and the message
            # is genuinely useful to the seller. Anything else is our problem,
            # not theirs.
            if exc.status == 422 and exc.message:
                return {'ok': False, 'error': exc.message, 'blocking': True}
            return {
                'ok': False,
                'error': _(
                    'India Post rates are unavailable right now. Please try '
                    'again in a few minutes.'
                ),
                'blocking': False,
            }
        except Exception:  # pragma: no cover - defensive for a public route
            _logger.exception('India Post tariff lookup raised unexpectedly')
            return {
                'ok': False,
                'error': _(
                    'India Post rates are unavailable right now. Please try '
                    'again in a few minutes.'
                ),
                'blocking': False,
            }

    @api.model
    def _ip_store_quote(self, cache_key, source_pincode, destination_pincode,
                        weight_g, length_cm, breadth_cm, height_cm, vas_key,
                        payload, settings):
        _distance, distance_display = ipc.as_distance(payload.get('distance_km'))
        minutes = settings['indiapost_tariff_cache_minutes'] or 30
        vals = {
            'cache_key': cache_key,
            'source_pincode': source_pincode,
            'destination_pincode': destination_pincode,
            'weight_g': weight_g,
            'length_cm': length_cm,
            'breadth_cm': breadth_cm,
            'height_cm': height_cm,
            'vas_key': vas_key,
            'product_code': payload.get('product_code') or '',
            'chargeable_weight_g': int(
                ipc.as_amount(payload.get('chargeable_weight'), weight_g)),
            'is_local': bool(payload.get('is_local')),
            'distance_display': distance_display,
            'base_tariff': ipc.as_amount(payload.get('base_tariff')),
            'vas_charges': ipc.as_amount(payload.get('vas_charges')),
            'cgst': ipc.as_amount(payload.get('cgst')),
            'sgst': ipc.as_amount(payload.get('sgst')),
            'total_tax': ipc.as_amount(payload.get('total_tax')),
            'final_amount': ipc.as_amount(payload.get('final_amount')),
            'delivery_type': payload.get('delivery_type') or '',
            'payload': json.dumps(payload, default=str)[:20000],
            'expires_at': fields.Datetime.add(fields.Datetime.now(),
                                              minutes=minutes),
        }
        Cache = self.env['logistics.indiapost.tariff.cache'].sudo()
        existing = Cache.search([('cache_key', '=', cache_key)], limit=1)
        if existing:
            existing.write(vals)
            return existing
        return Cache.create(vals)

    @api.model
    def _ip_quote_from_cache(self, entry):
        try:
            payload = json.loads(entry.payload or '{}')
        except (ValueError, TypeError):
            payload = {}
        vas_details = payload.get('vas_details') or {}
        return {
            'product_code': entry.product_code,
            'chargeable_weight_g': entry.chargeable_weight_g,
            'is_local': entry.is_local,
            'distance_display': entry.distance_display,
            'base_tariff': entry.base_tariff,
            'vas_charges': entry.vas_charges,
            'vas_details': {key: ipc.as_amount(value)
                            for key, value in vas_details.items()},
            'cgst': entry.cgst,
            'sgst': entry.sgst,
            'total_tax': entry.total_tax,
            'final_amount': entry.final_amount,
            'delivery_type': entry.delivery_type,
            'source_pincode': entry.source_pincode,
            'destination_pincode': entry.destination_pincode,
            'is_document': entry.product_code == ipc.PRODUCT_DOC,
        }
