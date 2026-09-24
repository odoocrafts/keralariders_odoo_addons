from odoo import models, fields, api, _
from odoo.exceptions import UserError

import logging
import re

_logger = logging.getLogger(__name__)

# India Post ``state_name`` labels that do not exactly match Odoo
# ``res.country.state`` names for India (``base`` / ``res.country.state.csv``).
# Values are candidate Odoo names tried in order (first existing wins).
_IN_STATE_ALIASES = {
    'andaman and nicobar islands': ['Andaman and Nicobar'],
    'andaman and nicobar': ['Andaman and Nicobar'],
    'delhi': ['Delhi'],
    'nct of delhi': ['Delhi'],
    'national capital territory of delhi': ['Delhi'],
    'pondicherry': ['Puducherry'],
    'puducherry': ['Puducherry'],
    'orissa': ['Odisha'],
    'odisha': ['Odisha'],
    'uttaranchal': ['Uttarakhand'],
    'uttarakhand': ['Uttarakhand'],
    'jammu & kashmir': ['Jammu and Kashmir'],
    'jammu and kashmir': ['Jammu and Kashmir'],
    'dadra and nagar haveli and daman and diu': [
        'Dadra and Nagar Haveli and Daman and Diu',
        'Dadra and Nagar Haveli',
        'Daman and Diu',
    ],
    'dadra & nagar haveli and daman & diu': [
        'Dadra and Nagar Haveli and Daman and Diu',
        'Dadra and Nagar Haveli',
        'Daman and Diu',
    ],
    'dadra and nagar haveli': ['Dadra and Nagar Haveli'],
    'daman and diu': ['Daman and Diu'],
    'lakshadweep': ['Lakshadweep'],
    'lakshadweep islands': ['Lakshadweep'],
    # Odoo 19 base may lack Ladakh; fall back to J&K when absent.
    'ladakh': ['Ladakh', 'Jammu and Kashmir'],
}


def _normalize_in_state_label(name):
    """Lowercase, ``&``→``and``, collapse space, strip trailing ``islands``."""
    text = (name or '').strip().lower()
    if not text:
        return ''
    text = text.replace('&', ' and ')
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'\s+islands$', '', text).strip()
    return text

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
        """Match India Post ``state_name`` onto ``res.country.state`` (India).

        Order: alias table → case-insensitive exact name → light normalize
        (strip ``Islands``, ``&`` vs ``and``) against existing India states.
        Does not create ``res.country.state`` rows.
        """
        name = (state_name or '').strip()
        if not name:
            return self.env['res.country.state'].browse()
        india = self.env.ref('base.in')
        State = self.env['res.country.state'].sudo()
        domain_in = [('country_id', '=', india.id)]

        def _by_exact_name(label):
            label = (label or '').strip()
            if not label:
                return State.browse()
            return State.search(
                domain_in + [('name', '=ilike', label)], limit=1,
            )

        # 1) Alias table (normalized key → candidate Odoo names).
        normalized = _normalize_in_state_label(name)
        for candidate in _IN_STATE_ALIASES.get(normalized, ()):
            state = _by_exact_name(candidate)
            if state:
                return state

        # 2) Case-insensitive exact match on the raw India Post label.
        state = _by_exact_name(name)
        if state:
            return state

        # 3) Light normalize: compare against every India state name.
        if normalized:
            for st in State.search(domain_in):
                if _normalize_in_state_label(st.name) == normalized:
                    return st
            # Longest Odoo state name contained in the India Post label
            # (e.g. merged Dadra/Daman UT → Dadra and Nagar Haveli).
            best = State.browse()
            best_len = 0
            for st in State.search(domain_in):
                st_norm = _normalize_in_state_label(st.name)
                if not st_norm:
                    continue
                if st_norm in normalized and len(st_norm) > best_len:
                    best = st
                    best_len = len(st_norm)
            if best:
                return best
        return State.browse()

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
                raise UserError(_('%s is not a valid delivery pincode.') % (pincode or ''))
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
                raise UserError(_('%s is not a valid delivery pincode.') % pin)
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
                if ipc.is_pincode_not_found_message(exc.message or str(exc)):
                    raise UserError(
                        _('%s is not a valid delivery pincode.') % pin
                    ) from exc
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
                if ipc.is_pincode_not_found_message(message):
                    raise UserError(
                        _('%s is not a valid delivery pincode.') % pin
                    ) from exc
                raise UserError(
                    _('%s is not a valid delivery pincode.') % pin
                ) from exc
            return empty

        if not office:
            if raise_if_missing:
                raise UserError(_('%s is not a valid delivery pincode.') % pin)
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