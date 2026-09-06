"""Pincode to post office resolution, cached locally.

Booking needs an 8-digit ``pickup_dropoff_office_id``, which only the
pincode-search endpoint can give us. Results are cached in this model so a busy
day of bookings does not mean one API round trip per shipment.
"""

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

import logging

from . import indiapost_common as ipc
from .indiapost_client import IndiapostApiError

_logger = logging.getLogger(__name__)

PINCODE_SEARCH_PATH = '/v1/pincode-search'
# The endpoint defaults to 50 records and Delhi 110001 returns 22, so one page
# is enough in practice; skip/limit work but are undocumented.
PINCODE_SEARCH_LIMIT = 100

# District-headquarters offices for all 14 Kerala districts, each confirmed
# live against /v1/pincode-search. Seeded so a cold cache still resolves a
# bookable office for every district KeralaXpress collects from, even if the
# API is briefly unreachable. Tuples are
# (pincode, office_id, office_name, office_type_code, district).
KERALA_HQ_OFFICES = [
    ('671121', '22360040', 'Kasaragod HO', 'HPO', 'Kasargod'),
    ('670001', '22840007', 'Delivery Centre Kannur', 'IDC', 'Kannur'),
    ('673121', '22360036', 'Kalpetta HO', 'HPO', 'Wayanad'),
    ('673001', '22840008', 'Delivery Centre Kozhikode', 'IDC', 'Kozhikode'),
    ('676505', '22360041', 'Malappuram HO', 'HPO', 'Malappuram'),
    ('678001', '22840002', 'DELIVERY CENTRE PALAKKAD', 'IDC', 'Palakkad'),
    ('680001', '22360032', 'Thrissur HO', 'HPO', 'Thrissur'),
    ('682001', '22360020', 'Kochi HO', 'HPO', 'Ernakulam'),
    ('685603', '22660631', 'Idukki Painavu SO', 'SPO', 'Idukki'),
    ('686001', '22360025', 'Kottayam HO', 'HPO', 'Kottayam'),
    ('688001', '22840001', 'IDC Alappuzha', 'IDC', 'Alappuzha'),
    ('689645', '22360002', 'Pathanamthitta HO', 'HPO', 'Pathanamthitta'),
    ('691001', '22840014', 'DC Kollam', 'IDC', 'Kollam'),
    ('695001', '22840005', 'DC Thiruvananthapuram GPO', 'IDC', 'Thiruvananthapuram'),
]


class IndiapostOffice(models.Model):
    _name = 'logistics.indiapost.office'
    _description = 'India Post Office'
    _order = 'pincode, is_preferred desc, office_name'
    _rec_name = 'office_name'

    pincode = fields.Char(string='Pincode', required=True, index=True)
    office_id = fields.Char(string='Office Id', required=True, index=True)
    office_name = fields.Char(string='Office Name', required=True)
    office_type_code = fields.Char(string='Office Type', index=True)
    state_name = fields.Char(string='State')
    city_name = fields.Char(string='City')
    taluk_name = fields.Char(string='Taluk')
    village_name = fields.Char(string='Village')
    delivery_office_flag = fields.Boolean(string='Delivery Office')
    is_rolled_out = fields.Boolean(string='Rolled Out')
    is_bookable = fields.Boolean(
        string='Bookable', index=True,
        help='A delivery office that is not a Branch Post Office.',
    )
    is_preferred = fields.Boolean(
        string='Preferred for Pincode', index=True,
        help='The office this pincode resolves to when booking.',
    )
    spds_id = fields.Char(string='SPDS Id')
    idc_id = fields.Char(string='IDC Id')
    idc_name = fields.Char(string='IDC Name')
    source = fields.Selection(
        [('api', 'API'), ('seed', 'Seeded')], string='Source', default='api',
    )
    last_synced = fields.Datetime(string='Last Synced', readonly=True)

    _sql_constraints = [
        ('pincode_office_uniq', 'UNIQUE (pincode, office_id)',
         'This office is already cached for that pincode.'),
    ]

    @api.constrains('pincode')
    def _check_pincode(self):
        for record in self:
            try:
                ipc.normalize_pincode(record.pincode)
            except ipc.IndiapostDataError as exc:
                raise ValidationError(str(exc)) from exc

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    @api.model
    def _ip_fetch_offices(self, pincode, settings=None):
        """Raw office records for a pincode, straight from the API.

        The pincode must already be 6 digits: a 5-digit value makes the
        endpoint do a prefix match and return unrelated offices instead of an
        error, and a nonexistent pincode returns HTTP 200 with no records at
        all, so neither can be detected from the status code.
        """
        Client = self.env['logistics.indiapost.client']
        response = Client.call(
            'GET', PINCODE_SEARCH_PATH,
            params={
                'pincode': pincode,
                'office-type': 'post',
                'limit': PINCODE_SEARCH_LIMIT,
            },
            operation='pincode-search',
            settings=settings,
        )
        return ipc.unwrap_records(response.payload)

    @api.model
    def _ip_sync_pincode(self, pincode, settings=None):
        """Refresh the cache for one pincode and return its office records."""
        records = self._ip_fetch_offices(pincode, settings=settings)
        if not records:
            return self.browse()

        bookable = sorted(
            [record for record in records if ipc.office_is_bookable(record)],
            key=ipc.office_sort_key,
        )
        preferred_office_id = str(bookable[0].get('office_id')) if bookable else None

        now = fields.Datetime.now()
        cached = self.sudo().search([('pincode', '=', pincode)])
        by_office_id = {office.office_id: office for office in cached}
        touched = self.browse()
        for record in records:
            office_id = str(record.get('office_id') or '')
            if not office_id:
                continue
            vals = {
                'pincode': pincode,
                'office_id': office_id,
                'office_name': record.get('office_name') or office_id,
                'office_type_code': (record.get('office_type_code') or '').upper(),
                'state_name': record.get('state_name') or '',
                'city_name': record.get('city_name') or '',
                'taluk_name': record.get('taluk_name') or '',
                'village_name': record.get('village_name') or '',
                'delivery_office_flag': bool(record.get('delivery_office_flag')),
                'is_rolled_out': bool(record.get('is_rolled_out')),
                'is_bookable': ipc.office_is_bookable(record),
                'is_preferred': office_id == preferred_office_id,
                'spds_id': str(record.get('spds_id') or ''),
                'idc_id': str(record.get('idc_id') or ''),
                'idc_name': record.get('idc_name') or '',
                'source': 'api',
                'last_synced': now,
            }
            existing = by_office_id.get(office_id)
            if existing:
                existing.sudo().write(vals)
                touched |= existing
            else:
                touched |= self.sudo().create(vals)
        # Anything still flagged preferred but not chosen this time must be
        # cleared, otherwise two offices could both look preferred.
        (cached - touched).sudo().write({'is_preferred': False})
        return touched

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    @api.model
    def _ip_cache_is_fresh(self, office, settings=None):
        settings = settings or self.env['logistics.indiapost.client']._ip_settings()
        days = settings['indiapost_office_cache_days']
        if not days:
            return False
        if not office.last_synced:
            # Seeded rows have no sync timestamp and are always usable.
            return office.source == 'seed'
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        return office.last_synced >= cutoff

    @api.model
    def resolve_booking_office(self, pincode, settings=None, refresh=False,
                               raise_if_missing=True):
        """The office record a pincode should book through.

        Reads the cache first, calls the API when the cache is cold or stale,
        and falls back to whatever is cached if the API is unreachable — a
        30-day-old office id is far better than a failed booking.
        """
        pincode = ipc.normalize_pincode(pincode)
        cached = self.sudo().search([
            ('pincode', '=', pincode), ('is_bookable', '=', True),
        ], order='is_preferred desc, id', limit=1)
        if cached and not refresh and self._ip_cache_is_fresh(cached, settings):
            return cached

        try:
            self._ip_sync_pincode(pincode, settings=settings)
        except (IndiapostApiError, UserError) as exc:
            if cached:
                _logger.warning(
                    'India Post: pincode %s could not be refreshed (%s); '
                    'using the cached office %s.',
                    pincode, exc, cached.office_id,
                )
                return cached
            if raise_if_missing:
                raise
            return self.browse()

        fresh = self.sudo().search([
            ('pincode', '=', pincode), ('is_bookable', '=', True),
        ], order='is_preferred desc, id', limit=1)
        if not fresh and raise_if_missing:
            raise UserError(_(
                'India Post has no bookable post office for pincode %s. '
                'Speed Post cannot pick up from or deliver to this pincode.'
            ) % pincode)
        return fresh

    @api.model
    def resolve_office_id(self, pincode, settings=None):
        """Just the 8-digit office id for a pincode."""
        return self.resolve_booking_office(pincode, settings=settings).office_id

    # ------------------------------------------------------------------
    # Seed data
    # ------------------------------------------------------------------
    @api.model
    def _ip_seed_kerala_offices(self):
        """Insert the verified Kerala district-HQ offices if absent.

        Idempotent, and it never overwrites a row the API has refreshed: the
        live data always wins.
        """
        created = 0
        for pincode, office_id, name, type_code, district in KERALA_HQ_OFFICES:
            if self.sudo().search_count([
                ('pincode', '=', pincode), ('office_id', '=', office_id),
            ]):
                continue
            self.sudo().create({
                'pincode': pincode,
                'office_id': office_id,
                'office_name': name,
                'office_type_code': type_code,
                'state_name': 'KERALA',
                'city_name': district,
                'delivery_office_flag': True,
                'is_rolled_out': True,
                'is_bookable': True,
                'is_preferred': not self.sudo().search_count([
                    ('pincode', '=', pincode), ('is_preferred', '=', True),
                ]),
                'source': 'seed',
            })
            created += 1
        return created

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------
    @api.model
    def cron_refresh_offices(self, limit=200):
        """Re-sync the oldest cached pincodes so ids never go badly stale."""
        Client = self.env['logistics.indiapost.client']
        if not Client._ip_is_configured():
            return 0
        settings = Client._ip_settings()
        days = settings['indiapost_office_cache_days'] or 30
        cutoff = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        stale = self.sudo().search(
            [('is_preferred', '=', True), '|',
             ('last_synced', '=', False), ('last_synced', '<', cutoff)],
            order='last_synced asc nulls first', limit=limit,
        )
        refreshed = 0
        for pincode in dict.fromkeys(stale.mapped('pincode')):
            try:
                self._ip_sync_pincode(pincode, settings=settings)
                refreshed += 1
            except (IndiapostApiError, UserError) as exc:
                _logger.warning(
                    'India Post: refreshing pincode %s failed: %s', pincode, exc)
        return refreshed

    def action_ip_refresh(self):
        """Form button: re-read this pincode from the API."""
        for pincode in dict.fromkeys(self.mapped('pincode')):
            self._ip_sync_pincode(pincode)
        return True

    @api.model
    def action_ip_sync_kerala_hubs(self):
        """Warm the cache with every pincode attached to a KeralaXpress hub.

        Useful right after install: pickups all originate in Kerala, so these
        are the office ids the booking flow will ask for first.
        """
        Client = self.env['logistics.indiapost.client']
        Client._ip_require_configured()
        settings = Client._ip_settings()
        pincodes = set()
        sender_pin = (settings['indiapost_sender_pincode'] or '').strip()
        if sender_pin:
            pincodes.add(sender_pin)
        for seller in self.env['logistics.seller'].sudo().search(
                [('fulfilment_method', '=', 'indiapost')]):
            zipcode = (seller.zip or '').strip()
            if zipcode:
                pincodes.add(zipcode)
        synced = 0
        failed = []
        for pincode in sorted(pincodes):
            try:
                self._ip_sync_pincode(
                    ipc.normalize_pincode(pincode), settings=settings)
                synced += 1
            except (ipc.IndiapostDataError, IndiapostApiError, UserError) as exc:
                failed.append('%s (%s)' % (pincode, exc))
        message = _('Cached post offices for %s pincode(s).') % synced
        if failed:
            message += '\n' + _('Could not resolve: %s') % ', '.join(failed[:10])
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('India Post office cache'),
                'message': message,
                'type': 'warning' if failed else 'success',
                'sticky': bool(failed),
            },
        }
