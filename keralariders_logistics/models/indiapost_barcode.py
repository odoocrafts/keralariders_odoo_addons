"""Allocation of India Post article barcodes (AWBs).

India Post allots ranges of serials to a bulk customer. A duplicate barcode is
a real operational failure — two parcels with the same number — and the label
endpoint validates nothing, so uniqueness is entirely our problem.

Allocation therefore takes a row lock on the range before bumping its counter,
and the barcode column carries a unique index as a second line of defence.
"""

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

import logging

from . import indiapost_common as ipc

_logger = logging.getLogger(__name__)


class IndiapostBarcodeRange(models.Model):
    _name = 'logistics.indiapost.barcode.range'
    _description = 'India Post Barcode Range'
    _order = 'sequence, id'

    name = fields.Char(string='Name', required=True)
    sequence = fields.Integer(string='Priority', default=10)
    active = fields.Boolean(string='Active', default=True)
    environment = fields.Selection(
        [('sandbox', 'Sandbox / UAT'), ('production', 'Production')],
        string='Environment', required=True, default='sandbox',
        help='Only ranges matching the configured India Post environment are used.',
    )
    prefix = fields.Char(
        string='Prefix', required=True, size=2,
        help='Two letters at positions 1-2 of the barcode, e.g. ET.',
    )
    start_serial = fields.Integer(string='First Serial', required=True)
    end_serial = fields.Integer(string='Last Serial', required=True)
    next_serial = fields.Integer(
        string='Next Serial', required=True, copy=False,
        help='The serial that will be handed out next. Never edit this while '
             'bookings are running.',
    )
    barcode_ids = fields.One2many(
        'logistics.indiapost.barcode', 'range_id', string='Allocated Barcodes',
    )
    total_count = fields.Integer(string='Range Size', compute='_compute_counts')
    allocated_count = fields.Integer(string='Allocated', compute='_compute_counts')
    # Stored, and therefore computed separately from the display-only counts:
    # _ip_pick_range searches on is_exhausted.
    remaining_count = fields.Integer(string='Remaining', compute='_compute_stock',
                                     store=True)
    is_exhausted = fields.Boolean(string='Exhausted', compute='_compute_stock',
                                  store=True)
    low_stock_threshold = fields.Integer(
        string='Low Stock Warning', default=50,
        help='Log a warning once fewer than this many barcodes remain.',
    )
    first_barcode = fields.Char(string='First Barcode', compute='_compute_preview')
    last_barcode = fields.Char(string='Last Barcode', compute='_compute_preview')

    _sql_constraints = [
        ('serial_order', 'CHECK (end_serial >= start_serial)',
         'The last serial must not be lower than the first serial.'),
    ]

    @api.depends('start_serial', 'end_serial', 'next_serial')
    def _compute_counts(self):
        for rng in self:
            rng.total_count = max(rng.end_serial - rng.start_serial + 1, 0)
            allocated = max(rng.next_serial - rng.start_serial, 0)
            rng.allocated_count = min(allocated, rng.total_count)

    @api.depends('end_serial', 'next_serial')
    def _compute_stock(self):
        for rng in self:
            rng.remaining_count = max(rng.end_serial - rng.next_serial + 1, 0)
            rng.is_exhausted = rng.remaining_count <= 0

    @api.depends('prefix', 'start_serial', 'end_serial')
    def _compute_preview(self):
        for rng in self:
            try:
                rng.first_barcode = ipc.build_barcode(rng.prefix, rng.start_serial)
                rng.last_barcode = ipc.build_barcode(rng.prefix, rng.end_serial)
            except ipc.IndiapostDataError:
                rng.first_barcode = rng.last_barcode = False

    @api.constrains('prefix', 'start_serial', 'end_serial', 'next_serial')
    def _check_range(self):
        for rng in self:
            prefix = (rng.prefix or '').strip().upper()
            if len(prefix) != 2 or not prefix.isalpha():
                raise ValidationError(_(
                    'The barcode prefix must be exactly two letters (got "%s").'
                ) % (rng.prefix or ''))
            if rng.start_serial < 1 or rng.end_serial > 99999999:
                raise ValidationError(_(
                    'Serials must fit in 8 digits, i.e. between 1 and 99999999.'
                ))
            if not (rng.start_serial <= rng.next_serial <= rng.end_serial + 1):
                raise ValidationError(_(
                    'The next serial must sit inside the range (%(start)s to '
                    '%(end)s), or one past the end when exhausted.'
                ) % {'start': rng.start_serial, 'end': rng.end_serial})

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('prefix'):
                vals['prefix'] = vals['prefix'].strip().upper()
            if not vals.get('next_serial'):
                vals['next_serial'] = vals.get('start_serial') or 1
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('prefix'):
            vals = dict(vals, prefix=vals['prefix'].strip().upper())
        return super().write(vals)

    # ------------------------------------------------------------------
    # Allocation
    # ------------------------------------------------------------------
    @api.model
    def _ip_pick_range(self, environment):
        """The highest priority active range with barcodes left."""
        return self.sudo().search([
            ('environment', '=', environment),
            ('is_exhausted', '=', False),
        ], order='sequence, id', limit=1)

    def _ip_next_serial(self):
        """Reserve and return the next serial, serialising concurrent callers.

        ``FOR UPDATE`` on the range row means a second transaction asking for a
        barcode waits here rather than reading the same counter.
        """
        self.ensure_one()
        self.env.cr.execute(
            """SELECT next_serial, end_serial
                 FROM logistics_indiapost_barcode_range
                WHERE id = %s
                  FOR UPDATE""",
            (self.id,),
        )
        row = self.env.cr.fetchone()
        if not row:
            raise UserError(_('Barcode range %s no longer exists.') % self.display_name)
        next_serial, end_serial = row
        if next_serial > end_serial:
            raise UserError(_(
                'Barcode range "%s" is exhausted. Ask India Post for a new '
                'range and add it under Logistics > India Post > Barcode Ranges.'
            ) % self.display_name)
        self.env.cr.execute(
            """UPDATE logistics_indiapost_barcode_range
                  SET next_serial = %s
                WHERE id = %s""",
            (next_serial + 1, self.id),
        )
        self.invalidate_recordset(['next_serial', 'remaining_count', 'is_exhausted'])
        remaining = end_serial - next_serial
        if 0 < remaining <= (self.low_stock_threshold or 0):
            _logger.warning(
                'India Post barcode range %s has only %s barcodes left.',
                self.display_name, remaining,
            )
        return next_serial

    @api.model
    def allocate(self, shipment=None, environment=None):
        """Allocate exactly one barcode, optionally pinned to a shipment.

        A shipment keeps the same barcode for its whole life: if a booking is
        rejected we re-send the barcode we already reserved rather than burning
        another one, which is what makes retries idempotent.
        """
        Barcode = self.env['logistics.indiapost.barcode'].sudo()
        if shipment:
            existing = Barcode.search([('shipment_id', '=', shipment.id)], limit=1)
            if existing:
                return existing

        if not environment:
            environment = self.env['logistics.indiapost.client']._ip_settings()[
                'indiapost_environment']
        rng = self._ip_pick_range(environment)
        if not rng:
            raise UserError(_(
                'No India Post barcode range is available for the %s '
                'environment. Add one under Logistics > India Post > Barcode '
                'Ranges before booking.'
            ) % environment)

        serial = rng._ip_next_serial()
        return Barcode.create({
            'range_id': rng.id,
            'serial': serial,
            'barcode': ipc.build_barcode(rng.prefix, serial),
            'shipment_id': shipment.id if shipment else False,
            'state': 'reserved',
        })

    def action_ip_view_barcodes(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Allocated Barcodes'),
            'res_model': 'logistics.indiapost.barcode',
            'view_mode': 'list,form',
            'domain': [('range_id', '=', self.id)],
            'context': {'create': 0},
        }


class IndiapostBarcode(models.Model):
    _name = 'logistics.indiapost.barcode'
    _description = 'India Post Article Barcode'
    _order = 'serial desc, id desc'
    _rec_name = 'barcode'

    barcode = fields.Char(string='Barcode', required=True, index=True, copy=False)
    serial = fields.Integer(string='Serial', required=True, index=True)
    range_id = fields.Many2one(
        'logistics.indiapost.barcode.range', string='Range', required=True,
        ondelete='restrict', index=True,
    )
    shipment_id = fields.Many2one(
        'logistics.shipment', string='Shipment', ondelete='set null', index=True,
    )
    state = fields.Selection(
        [
            ('reserved', 'Reserved'),
            ('booked', 'Booked'),
            ('rejected', 'Rejected by India Post'),
            ('void', 'Void'),
        ],
        string='Status', default='reserved', required=True, index=True,
    )
    allocated_on = fields.Datetime(
        string='Allocated On', default=fields.Datetime.now, readonly=True,
    )
    booked_on = fields.Datetime(string='Booked On', readonly=True)
    note = fields.Char(string='Note')

    _sql_constraints = [
        ('barcode_uniq', 'UNIQUE (barcode)',
         'This India Post barcode has already been allocated. A barcode must '
         'never be issued twice.'),
        ('shipment_uniq', 'UNIQUE (shipment_id)',
         'A shipment can only hold one India Post barcode.'),
    ]

    @api.constrains('barcode')
    def _check_barcode(self):
        for record in self:
            if not ipc.barcode_is_wellformed(record.barcode):
                raise ValidationError(_(
                    '"%s" is not a valid India Post barcode. It must be two '
                    'letters, eight digits, a modulo-11 check digit and "IN".'
                ) % (record.barcode or ''))

    def _ip_mark_booked(self):
        self.write({'state': 'booked', 'booked_on': fields.Datetime.now()})

    def _ip_mark_rejected(self, note=None):
        self.write({'state': 'rejected', 'note': (note or '')[:255]})

    def action_ip_void(self):
        """Take a barcode out of circulation without freeing the serial."""
        for record in self:
            if record.state == 'booked':
                raise UserError(_(
                    'Barcode %s is already booked with India Post and cannot be '
                    'voided.'
                ) % record.barcode)
        self.write({'state': 'void'})
