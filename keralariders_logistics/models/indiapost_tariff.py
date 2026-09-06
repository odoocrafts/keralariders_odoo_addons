"""Speed Post tariff lookups, with a short-lived cache.

The public rate calculator is ``auth="public"``, so without a cache an
anonymous visitor could turn the page into a load generator against India
Post. Quotes are therefore cached on
(source pincode, destination pincode, weight band, dimensions, VAS flags) for a
configurable number of minutes.
"""

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

import hashlib
import json
import logging

from . import indiapost_common as ipc
from .indiapost_client import IndiapostApiError

_logger = logging.getLogger(__name__)

TARIFF_PATH = '/v1/speed-post/tariffs'

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
                      article_type=ipc.ARTICLE_TYPE_SPEED_POST):
        raw = '|'.join(str(part) for part in (
            environment, article_type, source_pincode, destination_pincode,
            weight_g, length_cm, breadth_cm, height_cm, vas_key,
        ))
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    @api.model
    def _ip_tariff_params(self, source_pincode, destination_pincode, weight_g,
                          length_cm, breadth_cm, height_cm,
                          insurance_value=0.0,
                          article_type=ipc.ARTICLE_TYPE_SPEED_POST, **flags):
        params = {
            'product-code': article_type,
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
        length = ipc.cm_to_int(length_cm)
        breadth = ipc.cm_to_int(breadth_cm)
        height = ipc.cm_to_int(height_cm)

        errors, warnings = ipc.validate_package(actual_g, length, breadth, height)
        if errors:
            raise ValidationError('\n'.join(errors))

        billed_g = ipc.band_weight(actual_g) if band else actual_g
        vas_key = self._ip_vas_key(insurance_value=insurance_value, **flags)
        cache_key = self._ip_cache_key(
            source_pincode, destination_pincode, billed_g, length, breadth,
            height, vas_key, settings['indiapost_environment'],
            article_type=article_type,
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
            response = self.env['logistics.indiapost.client'].call(
                'GET', TARIFF_PATH,
                params=self._ip_tariff_params(
                    source_pincode, destination_pincode, billed_g, length,
                    breadth, height, insurance_value=insurance_value,
                    article_type=article_type, **flags),
                operation='tariff', shipment=shipment, settings=settings,
            )
            entry = self._ip_store_quote(
                cache_key, source_pincode, destination_pincode, billed_g,
                length, breadth, height, vas_key, response.payload or {},
                settings,
            )

        quote = self._ip_quote_from_cache(entry)
        # The India Post figures stay untouched in the cache; any KeralaXpress
        # margin is applied on top here so the passthrough stays auditable.
        markup_percent = settings['indiapost_quote_markup_percent']
        markup_amount = round(quote['final_amount'] * markup_percent / 100.0, 2)
        quote.update({
            'ok': True,
            'cached': cached,
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
