from odoo import models, fields, api, _
from odoo.exceptions import UserError

import logging

_logger = logging.getLogger(__name__)

north_kerala_districts = {
    'kerala_district_1': 'kasargod',
    'kerala_district_2': 'kannur',
    'kerala_district_3': 'wayanad',
    'kerala_district_4': 'kozhikode',
    'kerala_district_5': 'malappuram',
    'kerala_district_6': 'palakkad',
}

central_district = {
    'kerala_district_7': 'thrissur',
}

south_kerala_districts = {
    'kerala_district_8': 'ernakulam',
    'kerala_district_9': 'idukki',
    'kerala_district_10': 'kottayam',
    'kerala_district_11': 'alappuzha',
    'kerala_district_12': 'pathanamthitta',
    'kerala_district_13': 'kollam',
    'kerala_district_14': 'thiruvananthapuram',
}
class Pincode(models.Model):
    _name = 'logistics.pincode'
    _description = 'Pincode'

    name = fields.Char(string='Pincode')
    district_id = fields.Many2one('logistics.district', string='District', compute="_compute_district_id")

    def _compute_district_id(self):
        for rec in self:
            district_id = self.env['logistics.district'].get_district_from_pincode(rec.name)['district_id']
            rec.district_id = district_id.id if district_id else False

    district_name = fields.Char(string='District Name', store=True)
    state_name = fields.Char(string='State Name', store=True)
    po_names = fields.Text(string='Post Office Names', store=True)

class District(models.Model):
    _name = 'logistics.district'
    _order="name"
    _description = 'District'

    name = fields.Char(string='District Name')
    state_id = fields.Many2one('res.country.state', string='State')

    @api.model
    def get_district_from_pincode(self, pincode):
        """Get district and state from the Kerala hub ``logistics.pincode`` table."""
        self.env.cr.execute(
            "SELECT district_name, state_name FROM logistics_pincode WHERE name = %s LIMIT 1",
            (pincode,)
        )
        result = self.env.cr.fetchone()
        if result:
            district_name, state_name = result
            district_id = self.search([('name', 'ilike', district_name)], limit=1)
            # CSV uses KASARAGOD; seeded district is named Kasargod
            if not district_id and district_name and district_name.upper() == 'KASARAGOD':
                district_id = self.search([('name', 'ilike', 'Kasargod')], limit=1)
            return {
                'district_id': district_id,
                'district_name': district_name,
                'state_name': state_name,
            }
        return {
            'district_id': False,
            'district_name': '',
            'state_name': '',
        }

    @api.model
    def _find_indian_state(self, state_name):
        """Match India Post ``state_name`` onto ``res.country.state`` (India)."""
        name = (state_name or '').strip()
        if not name:
            return self.env['res.country.state'].browse()
        india = self.env.ref('base.in')
        State = self.env['res.country.state'].sudo()
        return State.search([
            ('country_id', '=', india.id),
            ('name', 'ilike', name),
        ], limit=1)

    @api.model
    def _ensure_district_for_locality(self, city_name, state_name):
        """Find or create a ``logistics.district`` for an India Post locality.

        Does **not** write into ``logistics.pincode`` (hub serviceability stays
        Kerala-only). National rows are created on demand so a shipment can
        store destination district/state.
        """
        locality = (city_name or '').strip()
        if not locality:
            raise UserError(_(
                'India Post returned no city for this pincode, so the '
                'destination district cannot be set.'
            ))
        state = self._find_indian_state(state_name)
        if not state:
            raise UserError(_(
                'India Post returned an unrecognized state "%s" for this '
                'pincode.'
            ) % (state_name or ''))
        display = locality.title() if locality.isupper() else locality
        District = self.sudo()
        district = District.search([
            ('name', 'ilike', locality),
            ('state_id', '=', state.id),
        ], limit=1)
        if not district and locality.upper() == 'KASARAGOD':
            district = District.search([
                ('name', 'ilike', 'Kasargod'),
                ('state_id', '=', state.id),
            ], limit=1)
        if district:
            return district
        return District.create({
            'name': display,
            'state_id': state.id,
        })

    @api.model
    def resolve_destination_from_pincode(self, pincode, allow_indiapost=False,
                                         raise_if_missing=False):
        """Resolve destination district/state for order create / bulk / API.

        1. Kerala ``logistics.pincode`` / :meth:`get_district_from_pincode`.
        2. When ``allow_indiapost`` and that misses: sync
           ``logistics.indiapost.office`` via ``resolve_booking_office``
           (``/v1/pincode-search``, TTL cache) and map ``city_name`` (else
           ``taluk_name``) + ``state_name`` onto a district (created on demand).

        Never inserts national PINs into the hub ``logistics.pincode`` table.
        """
        pin = (pincode or '').strip()
        empty = {
            'district_id': self.browse(),
            'state_id': self.env['res.country.state'].browse(),
            'district_name': '',
            'state_name': '',
            'source': False,
        }
        if not pin:
            if raise_if_missing:
                raise UserError(_('Unknown pincode %s') % (pincode or ''))
            return empty

        local = self.get_district_from_pincode(pin)
        district = local.get('district_id')
        if district:
            return {
                'district_id': district,
                'state_id': district.state_id,
                'district_name': district.name or local.get('district_name') or '',
                'state_name': (
                    district.state_id.name if district.state_id
                    else (local.get('state_name') or '')
                ),
                'source': 'local',
            }

        if not allow_indiapost:
            if raise_if_missing:
                raise UserError(_('Unknown pincode %s') % pin)
            return empty

        from . import indiapost_common as ipc
        from .indiapost_client import IndiapostApiError

        try:
            pin = ipc.normalize_pincode(pin)
        except ipc.IndiapostDataError as exc:
            if raise_if_missing:
                raise UserError(str(exc)) from exc
            return empty

        Office = self.env['logistics.indiapost.office'].sudo()
        try:
            office = Office.resolve_booking_office(pin, raise_if_missing=True)
        except IndiapostApiError as exc:
            _logger.warning(
                'India Post pincode-search failed for %s: %s', pin, exc)
            if raise_if_missing:
                raise UserError(_(
                    'India Post could not verify pincode %s (service '
                    'unavailable). Try again later.'
                ) % pin) from exc
            return empty
        except UserError as exc:
            # Empty / unserviceable PIN from India Post, or a UserError already
            # raised by the client. Do not invent a district.
            if raise_if_missing:
                message = str(exc.args[0] if exc.args else exc)
                if 'could not verify' in message.lower():
                    raise
                raise UserError(_('Unknown pincode %s') % pin) from exc
            return empty

        if not office:
            if raise_if_missing:
                raise UserError(_('Unknown pincode %s') % pin)
            return empty

        city = (office.city_name or office.taluk_name or '').strip()
        state_name = (office.state_name or '').strip()
        district = self._ensure_district_for_locality(city, state_name)
        return {
            'district_id': district,
            'state_id': district.state_id,
            'district_name': district.name,
            'state_name': district.state_id.name if district.state_id else state_name,
            'source': 'indiapost',
        }

    @api.model
    def get_district_zone(self, district_id):
        district_name = (district_id.name or "").lower()
        if district_name in central_district.values():
            return 'central'
        elif district_name in north_kerala_districts.values():
            return 'north'
        elif district_name in south_kerala_districts.values():
            return 'south'
        # else:
        #     raise UserError(f'Zone cannot be determined from the {district_name} district')

    @api.model
    def get_central_district_id(self):
        central_district_id = self.env.ref(f'keralariders_logistics.{list(central_district.keys())[0]}')
        return central_district_id