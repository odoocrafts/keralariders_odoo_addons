"""India Post seller-portal spreadsheet: one scan adjustment per shipment.

The export this reads (sheet headers, verified against a live download) is:

    article-number            India Post article / ARN / barcode. Match key.
    customer-bulk-reference   Our AWB when it is all digits (26090504). Used
                              only when the article number does not match.
    tarrif                    Billed amount India Post collected. Spelled
                              without the second f. Preferred over a re-quote.
                              Compared as-is with the pickup wallet debit.
                              Not ``base-amount`` (that row's pre-tax figure).
    weight                    Charged / physical weight in grams (197, 2930).
    booking-office-name       Booking office.
    booking-office-pin        Booking office PIN.
    booking-date-time         Booking timestamp as text (2026/10/01T15:41:01).

There is no dimension column on that export. Length / breadth / height are
read only when a sheet actually has ``length-cm`` / ``breadth-cm`` /
``height-cm`` (or ``length`` / ``breadth`` / ``height``, or
``article-length`` / ``article-breadth`` / ``article-height``). A billed
``tarrif`` wins when both a bill and dimensions are present. With no bill,
weight and all three dimensions are re-quoted through the existing tariff
function (Lakshadweep zone and the under-500 g Business Parcel rule live
there). COD columns are ignored. Nothing is rebooked.
"""

import base64
import io
import logging
import os

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.tools.float_utils import float_compare

from . import indiapost_common as ipc

_logger = logging.getLogger(__name__)

SCAN_IMPORT_GROUP_XMLIDS = (
    'keralariders_logistics.group_logistics_admin',
    'keralariders_logistics.group_staff_indiapost',
)

MAX_IMPORT_ROWS = 5000

# Seller-portal header spellings. ``tarrif`` is the billed amount.
HEADER_ARTICLE = 'article-number'
HEADER_BOOKING_REF = 'customer-bulk-reference'
HEADER_TARIFF = 'tarrif'
HEADER_TARIFF_ALT = 'tariff'
HEADER_WEIGHT = 'weight'
HEADER_BASE = 'base-amount'
HEADER_OFFICE = 'booking-office-name'
HEADER_OFFICE_PIN = 'booking-office-pin'
HEADER_BOOKED = 'booking-date-time'

_DIM_HEADER_ALIASES = {
    'length_cm': ('length-cm', 'length', 'article-length'),
    'breadth_cm': ('breadth-cm', 'breadth', 'article-breadth'),
    'height_cm': ('height-cm', 'height', 'article-height'),
}


def _check_scan_import_user(env):
    user = env.user
    if any(user.has_group(xmlid) for xmlid in SCAN_IMPORT_GROUP_XMLIDS):
        return
    raise AccessError(_(
        'Only an India Post administrator can import scan charges.'
    ))


def _xlsx_bytes(data):
    """Accept a Binary field value (base64 or raw workbook bytes)."""
    if not data:
        return b''
    if isinstance(data, str):
        data = data.encode()
    if data[:2] == b'PK':
        return data
    try:
        decoded = base64.b64decode(data)
    except (TypeError, ValueError):
        return data
    if decoded[:2] == b'PK':
        return decoded
    return data


def _cell_text(value):
    if value in (None, False):
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def _header_key(value):
    return _cell_text(value).strip().lower()


class IndiapostScanImport(models.Model):
    _name = 'logistics.indiapost.scan.import'
    _description = 'India Post Scan Import'
    _order = 'import_datetime desc, id desc'
    _rec_name = 'file_name'

    file_name = fields.Char(string='File name', required=True, readonly=True)
    file_data = fields.Binary(string='Spreadsheet', readonly=True, attachment=True)
    user_id = fields.Many2one(
        'res.users', string='Uploaded by', readonly=True, required=True,
        default=lambda self: self.env.user, index=True,
    )
    import_datetime = fields.Datetime(
        string='Uploaded on', readonly=True, required=True,
        default=fields.Datetime.now,
    )
    row_count = fields.Integer(string='Rows', readonly=True)
    posted_count = fields.Integer(string='Posted', readonly=True)
    skipped_count = fields.Integer(string='Skipped', readonly=True)
    error_count = fields.Integer(string='Errors', readonly=True)
    total_debit = fields.Monetary(
        string='Total debit', currency_field='currency_id', readonly=True,
    )
    total_credit = fields.Monetary(
        string='Total credit', currency_field='currency_id', readonly=True,
    )
    currency_id = fields.Many2one(
        'res.currency', required=True, readonly=True,
        default=lambda self: self.env.company.currency_id,
    )
    line_ids = fields.One2many(
        'logistics.indiapost.scan.import.line', 'import_id', string='Lines',
        readonly=True,
    )

    def action_open_scan_import_wizard(self):
        """List-header button. Works with no import selected."""
        _check_scan_import_user(self.env)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Upload scan spreadsheet'),
            'res_model': 'logistics.indiapost.scan.import.wizard',
            'view_mode': 'form',
            'target': 'new',
        }

    @api.model
    def create_from_xlsx(self, data, filename):
        """Parse ``data`` and post one adjustment per shipment. Returns the import."""
        _check_scan_import_user(self.env)
        raw = _xlsx_bytes(data)
        rows = self._ip_parse_scan_xlsx(raw)
        filename = os.path.basename(filename or 'indiapost_scan.xlsx') or 'indiapost_scan.xlsx'
        line_commands, totals = self._ip_post_scan_rows(rows)
        return self.create({
            'file_name': filename,
            'file_data': base64.b64encode(raw).decode('ascii'),
            'user_id': self.env.user.id,
            'import_datetime': fields.Datetime.now(),
            'row_count': totals['row_count'],
            'posted_count': totals['posted_count'],
            'skipped_count': totals['skipped_count'],
            'error_count': totals['error_count'],
            'total_debit': totals['total_debit'],
            'total_credit': totals['total_credit'],
            'line_ids': line_commands,
        })

    @api.model
    def _ip_parse_scan_xlsx(self, raw):
        """Return one dict per data row. Raises UserError on a bad workbook."""
        if not raw:
            raise UserError(_('Upload the India Post seller portal spreadsheet (.xlsx).'))
        try:
            import openpyxl
        except ImportError as exc:
            raise UserError(_(
                'Reading this spreadsheet needs the Python package openpyxl.'
            )) from exc
        try:
            workbook = openpyxl.load_workbook(
                io.BytesIO(raw), data_only=False, read_only=True)
        except Exception as exc:
            raise UserError(_(
                'Could not read this file as an Excel workbook (.xlsx).'
            )) from exc
        try:
            sheet = workbook.worksheets[0] if workbook.worksheets else None
            if sheet is None:
                raise UserError(_('The workbook has no sheet.'))
            parsed_rows = []
            header_index = None
            for excel_row, values in enumerate(sheet.iter_rows(values_only=True), start=1):
                cells = list(values or [])
                if header_index is None:
                    header_index = self._ip_header_index(cells)
                    continue
                if not any(_cell_text(cell) for cell in cells):
                    continue
                parsed_rows.append(self._ip_parse_data_row(header_index, cells, excel_row))
                if len(parsed_rows) > MAX_IMPORT_ROWS:
                    raise UserError(_(
                        'This spreadsheet has more than %s data rows.'
                    ) % MAX_IMPORT_ROWS)
            return parsed_rows
        finally:
            workbook.close()

    @api.model
    def _ip_header_index(self, cells):
        index = {}
        for position, header in enumerate(cells):
            key = _header_key(header)
            if key and key not in index:
                index[key] = position
        if HEADER_ARTICLE not in index:
            raise UserError(_(
                'This spreadsheet has no article-number column. Upload the '
                'India Post seller portal export.'
            ))
        return index

    @api.model
    def _ip_parse_data_row(self, header_index, cells, excel_row):
        def take(header):
            position = header_index.get(header)
            if position is None or position >= len(cells):
                return None
            return cells[position]

        tariff_header = HEADER_TARIFF if HEADER_TARIFF in header_index else HEADER_TARIFF_ALT
        billed = ipc.as_amount(take(tariff_header))
        if billed < 0:
            billed = 0.0
        length = breadth = height = 0
        for key, aliases in _DIM_HEADER_ALIASES.items():
            raw_dim = None
            for alias in aliases:
                if alias in header_index:
                    raw_dim = take(alias)
                    break
            if key == 'length_cm':
                length = ipc.cm_to_int(raw_dim)
            elif key == 'breadth_cm':
                breadth = ipc.cm_to_int(raw_dim)
            else:
                height = ipc.cm_to_int(raw_dim)
        weight_g = self._ip_parse_grams(take(HEADER_WEIGHT))
        return {
            'excel_row': excel_row,
            'article': _cell_text(take(HEADER_ARTICLE)).upper(),
            'booking_ref': _cell_text(take(HEADER_BOOKING_REF)),
            'billed': billed,
            'base_amount': ipc.as_amount(take(HEADER_BASE)),
            'weight_g': weight_g,
            'length_cm': length,
            'breadth_cm': breadth,
            'height_cm': height,
            'office_name': _cell_text(take(HEADER_OFFICE)),
            'office_pin': _cell_text(take(HEADER_OFFICE_PIN)),
            'booked_on': _cell_text(take(HEADER_BOOKED)),
        }

    @api.model
    def _ip_parse_grams(self, value):
        """``weight`` on the seller-portal export is grams (text or number)."""
        if value in (None, '', False):
            return 0
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            grams = int(round(float(value)))
            return grams if grams > 0 else 0
        text = str(value).strip().replace(',', '')
        if not text:
            return 0
        try:
            grams = int(round(float(text)))
        except (TypeError, ValueError):
            return 0
        return grams if grams > 0 else 0

    @api.model
    def _ip_match_shipment(self, article, booking_ref):
        """Article number first, then an all-digit booking reference (our AWB)."""
        Shipment = self.env['logistics.shipment']
        article = (article or '').strip().upper()
        ref = (booking_ref or '').strip()
        if article:
            found = Shipment.search([
                ('indiapost_article_number', '=ilike', article),
            ])
            found = found.filtered(
                lambda shipment: (shipment.indiapost_article_number or '').strip().upper()
                == article
            )
            if len(found) > 1:
                return Shipment, 'more than one shipment'
            if len(found) == 1:
                return found, False
        if ref.isdigit():
            found = Shipment.search([('name', '=', ref)])
            if len(found) > 1:
                return Shipment, 'more than one shipment'
            if len(found) == 1:
                return found, False
        if not article and not ref.isdigit():
            return Shipment, 'missing article number'
        return Shipment, 'article not found'

    @api.model
    def _ip_row_payable(self, shipment, row):
        """File ``tarrif`` when present, otherwise a tariff re-quote.

        Returns ``(payable, source, error_reason)``. ``source`` is
        ``portal_bill`` or ``tariff``.
        """
        billed = ipc.as_amount(row.get('billed'))
        if float_compare(billed, 0.0, precision_digits=2) > 0:
            payable = shipment._round_charge(billed) if shipment else round(billed, 2)
            return payable, 'portal_bill', False
        weight_g = int(row.get('weight_g') or 0)
        has_dims = bool(
            row.get('length_cm') and row.get('breadth_cm') and row.get('height_cm'))
        if not weight_g:
            return 0.0, False, 'no amount and no weight'
        if not has_dims:
            return 0.0, False, 'no amount and no dimensions'
        if not shipment:
            return 0.0, False, 'article not found'
        payable, error = self._ip_requote_portal_row(shipment, row)
        if error:
            return 0.0, False, error
        return payable, 'tariff', False

    @api.model
    def _ip_requote_portal_row(self, shipment, row):
        """Same tariff entry point as a scan re-quote. No booking, no COD."""
        origin = shipment._ip_origin_pincode_soft()
        dest = (shipment.shipping_to_zip or '').strip()
        if not origin or not dest:
            return 0.0, 'tariff re-quote failed'
        quote = self.env['logistics.indiapost.tariff'].quote_safe(
            origin, dest,
            article_type=shipment._ip_product(),
            weight_g=int(row['weight_g']),
            length_cm=int(row['length_cm']),
            breadth_cm=int(row['breadth_cm']),
            height_cm=int(row['height_cm']),
            insurance_value=shipment.indiapost_insurance_value,
            use_cache=True,
            shipment=shipment,
            **shipment._ip_vas_flags(),
        )
        if not quote or not quote.get('ok'):
            return 0.0, 'tariff re-quote failed'
        payable = shipment._round_charge(ipc.as_amount(quote.get('total_payable')))
        if float_compare(payable, 0.0, precision_digits=2) <= 0:
            return 0.0, 'tariff re-quote failed'
        return payable, False

    @api.model
    def _ip_post_scan_rows(self, rows):
        """Post adjustments. One bad row becomes an error line and does not abort."""
        commands = []
        posted_ids = set()
        totals = {
            'row_count': 0,
            'posted_count': 0,
            'skipped_count': 0,
            'error_count': 0,
            'total_debit': 0.0,
            'total_credit': 0.0,
        }
        for sequence, row in enumerate(rows, start=1):
            totals['row_count'] += 1
            vals = self._ip_import_one_row(row, sequence, posted_ids)
            state = vals['state']
            if state == 'posted':
                totals['posted_count'] += 1
                amount = vals.get('wallet_amount') or 0.0
                if float_compare(amount, 0.0, precision_digits=2) < 0:
                    totals['total_debit'] = round(
                        totals['total_debit'] + abs(amount), 2)
                elif float_compare(amount, 0.0, precision_digits=2) > 0:
                    totals['total_credit'] = round(
                        totals['total_credit'] + amount, 2)
            elif state == 'skipped':
                totals['skipped_count'] += 1
            else:
                totals['error_count'] += 1
            commands.append((0, 0, vals))
        currency = self.env.company.currency_id
        if currency:
            totals['total_debit'] = currency.round(totals['total_debit'])
            totals['total_credit'] = currency.round(totals['total_credit'])
        return commands, totals

    @api.model
    def _ip_import_one_row(self, row, sequence, posted_ids):
        shipment, match_error = self._ip_match_shipment(
            row.get('article'), row.get('booking_ref'))
        base = {
            'sequence': sequence,
            'excel_row': row.get('excel_row') or 0,
            'article_number': row.get('article') or '',
            'booking_ref': row.get('booking_ref') or '',
            'weight_g': int(row.get('weight_g') or 0),
            'base_amount': ipc.as_amount(row.get('base_amount')),
            'office_name': row.get('office_name') or '',
            'office_pin': row.get('office_pin') or '',
            'booked_on': row.get('booked_on') or '',
            'shipment_id': shipment.id if shipment else False,
        }
        if match_error or not shipment:
            base.update({
                'state': 'error',
                'reason': match_error or 'article not found',
                'file_amount': ipc.as_amount(row.get('billed')),
            })
            return base

        if shipment.id in posted_ids or shipment.indiapost_scan_adjusted \
                or shipment._ip_existing_scan_wallet_line():
            # Display the file bill if it has one. Do not re-quote and do not
            # post: this shipment already took its one adjustment.
            billed = ipc.as_amount(row.get('billed'))
            payable = shipment._round_charge(billed) if billed > 0 else 0.0
            previous = shipment._ip_portal_previous_charge()
            difference = (
                shipment._round_charge(payable - previous) if payable and previous
                else 0.0)
            existing = shipment._ip_existing_scan_wallet_line()
            base.update({
                'state': 'skipped',
                'reason': 'already adjusted',
                'previous_charge': previous,
                'file_amount': payable,
                'amount_source': 'portal_bill' if payable else False,
                'difference': difference,
                'wallet_txn_id': existing.id,
                'wallet_amount': existing.amount if existing else 0.0,
            })
            return base

        blocked, block_reason = shipment._ip_portal_adjustment_block()
        if blocked:
            payable = ipc.as_amount(row.get('billed'))
            if float_compare(payable, 0.0, precision_digits=2) <= 0:
                payable = 0.0
            base.update({
                'state': blocked,
                'reason': block_reason,
                'previous_charge': shipment._ip_portal_previous_charge(),
                'file_amount': shipment._round_charge(payable) if payable else 0.0,
                'amount_source': 'portal_bill' if payable else False,
            })
            return base

        payable, source, amount_error = self._ip_row_payable(shipment, row)
        if amount_error:
            base.update({
                'state': 'error',
                'reason': amount_error,
                'previous_charge': shipment._ip_portal_previous_charge(),
                'file_amount': payable or ipc.as_amount(row.get('billed')),
                'amount_source': source or False,
            })
            return base

        try:
            with self.env.cr.savepoint():
                outcome = shipment._ip_apply_portal_bill_adjustment(
                    payable,
                    weight_g=row.get('weight_g') or 0,
                    length_cm=row.get('length_cm') or 0,
                    breadth_cm=row.get('breadth_cm') or 0,
                    height_cm=row.get('height_cm') or 0,
                    source=source or 'portal_bill',
                )
        except Exception:
            _logger.exception(
                'India Post scan import failed for article %s',
                row.get('article') or row.get('booking_ref'),
            )
            base.update({
                'state': 'error',
                'reason': 'tariff re-quote failed' if source == 'tariff'
                else 'could not post the adjustment',
                'previous_charge': shipment._ip_portal_previous_charge(),
                'file_amount': payable,
                'amount_source': source or False,
            })
            return base

        wallet = outcome.get('wallet')
        wallet_amount = wallet.amount if wallet else 0.0
        if outcome.get('state') == 'posted' and shipment.id:
            posted_ids.add(shipment.id)
        base.update({
            'state': outcome.get('state') or 'error',
            'reason': outcome.get('reason') or '',
            'previous_charge': outcome.get('previous') or 0.0,
            'file_amount': outcome.get('payable') or 0.0,
            'amount_source': source or False,
            'difference': outcome.get('difference') or 0.0,
            'wallet_txn_id': wallet.id if wallet else False,
            'wallet_amount': wallet_amount if outcome.get('state') == 'posted' else 0.0,
        })
        return base


class IndiapostScanImportLine(models.Model):
    _name = 'logistics.indiapost.scan.import.line'
    _description = 'India Post Scan Import Line'
    _order = 'sequence, id'

    import_id = fields.Many2one(
        'logistics.indiapost.scan.import', string='Import', required=True,
        ondelete='cascade', index=True,
    )
    sequence = fields.Integer(default=10)
    excel_row = fields.Integer(string='Sheet row', readonly=True)
    article_number = fields.Char(string='Article number', readonly=True, index=True)
    booking_ref = fields.Char(string='Booking reference', readonly=True)
    shipment_id = fields.Many2one(
        'logistics.shipment', string='AWB', readonly=True, index=True,
        ondelete='set null',
    )
    seller_id = fields.Many2one(
        'logistics.seller', string='Seller', related='shipment_id.seller_id',
        store=True, readonly=True,
    )
    previous_charge = fields.Monetary(
        string='Previous charge', currency_field='currency_id', readonly=True,
    )
    file_amount = fields.Monetary(
        string='File / re-quoted amount', currency_field='currency_id',
        readonly=True,
    )
    amount_source = fields.Selection(
        [
            ('portal_bill', 'Seller portal bill'),
            ('tariff', 'Re-quoted'),
        ],
        string='Amount from', readonly=True,
    )
    difference = fields.Monetary(
        string='Difference', currency_field='currency_id', readonly=True,
        help='Billed or re-quoted amount minus the pickup debit. Positive is '
             'a wallet debit; negative is a credit. The wallet line itself is '
             'the opposite sign (a debit is a negative amount).',
    )
    wallet_txn_id = fields.Many2one(
        'logistics.wallet.transaction', string='Wallet move',
        readonly=True, ondelete='set null',
    )
    wallet_amount = fields.Monetary(
        string='Wallet amount', currency_field='currency_id', readonly=True,
        help='Amount posted on the seller wallet. Positive is a credit, '
             'negative is a debit.',
    )
    weight_g = fields.Integer(string='File weight (g)', readonly=True)
    base_amount = fields.Monetary(
        string='Base amount', currency_field='currency_id', readonly=True,
        help='Spreadsheet base-amount. Not used for the adjustment. The '
             'billed figure is the tarrif column.',
    )
    office_name = fields.Char(string='Booking office', readonly=True)
    office_pin = fields.Char(string='Office PIN', readonly=True)
    booked_on = fields.Char(string='Booking date', readonly=True)
    state = fields.Selection(
        [
            ('posted', 'Posted'),
            ('skipped', 'Skipped'),
            ('error', 'Error'),
        ],
        string='State', required=True, readonly=True, index=True,
    )
    reason = fields.Char(string='Reason', readonly=True)
    currency_id = fields.Many2one(
        related='import_id.currency_id', store=True, readonly=True,
    )
