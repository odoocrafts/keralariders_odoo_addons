from odoo import api, fields, models, _

from odoo.addons.keralariders_logistics.models.shipment import delivery_states

from ..tools.xlsx import DATE, INT, MONEY, TEXT, build_workbook
from .seller_ledger_line import LEDGER_CATEGORIES

STATE_LABELS = dict(delivery_states)
CATEGORY_LABELS = dict(LEDGER_CATEGORIES)


class KxReportService(models.AbstractModel):
    """Numbers behind every KeralaXpress report, as plain dicts.

    Backend wizards, the seller portal and the Excel builder all read from
    here so a figure means the same thing wherever it is shown. Callers
    decide the access: the portal passes a sudo environment together with
    the logged-in seller, never a seller chosen by the request.
    """
    _name = 'kx.report.service'
    _description = 'KeralaXpress Report Service'

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def _to_date(self, value):
        if not value:
            return False
        try:
            return fields.Date.to_date(value)
        except (TypeError, ValueError):
            return False

    @api.model
    def _currency(self):
        return self.env.company.currency_id

    @api.model
    def _round(self, amount):
        return self._currency().round(amount or 0.0)

    @api.model
    def _filter_meta(self, date_from, date_to, sellers=None, extra=None):
        meta = [
            (_('From'), fields.Date.to_string(date_from) if date_from else _('Beginning')),
            (_('To'), fields.Date.to_string(date_to) if date_to else _('Today')),
            (_('Seller'), ', '.join(sellers.mapped('name')) if sellers else _('All sellers')),
        ]
        meta.extend(extra or [])
        meta.append((_('Generated on'), fields.Datetime.to_string(fields.Datetime.now())))
        return meta

    # ------------------------------------------------------------------
    # Order reports (one shipment = one customer order)
    # ------------------------------------------------------------------
    @api.model
    def _order_domain(self, date_from=None, date_to=None, seller_ids=None, states=None):
        Shipment = self.env['logistics.shipment']
        domain = list(Shipment._domain_not_unbooked())
        date_from, date_to = self._to_date(date_from), self._to_date(date_to)
        if date_from:
            domain.append(('order_date', '>=', date_from))
        if date_to:
            domain.append(('order_date', '<=', date_to))
        if seller_ids:
            domain.append(('seller_id', 'in', list(seller_ids)))
        if states:
            domain.append(('state', 'in', list(states)))
        return domain

    @api.model
    def order_report(self, date_from=None, date_to=None, seller_ids=None, states=None):
        """Totals, status summary and seller-wise rows for the filters."""
        Shipment = self.env['logistics.shipment']
        domain = self._order_domain(date_from, date_to, seller_ids, states)
        groups = Shipment._read_group(
            domain, ['seller_id', 'state'],
            ['__count', 'total_order_value:sum', 'cod_amount:sum',
             'delivery_charges_total:sum'],
        )
        totals = {'count': 0, 'value': 0.0, 'delivered': 0, 'cancelled': 0,
                  'cod_amount': 0.0, 'charges': 0.0}
        by_state = {}
        by_seller = {}
        for seller, state, count, value, cod, charges in groups:
            value, cod, charges = value or 0.0, cod or 0.0, charges or 0.0
            totals['count'] += count
            totals['value'] += value
            totals['cod_amount'] += cod
            totals['charges'] += charges
            if state == 'delivered':
                totals['delivered'] += count
            elif state == 'cancelled':
                totals['cancelled'] += count
            st = by_state.setdefault(state, {'count': 0, 'value': 0.0})
            st['count'] += count
            st['value'] += value
            row = by_seller.setdefault(seller.id, {
                'seller_id': seller.id, 'seller': seller.name or '',
                'count': 0, 'value': 0.0, 'delivered': 0, 'cancelled': 0,
                'cod_amount': 0.0, 'charges': 0.0,
            })
            row['count'] += count
            row['value'] += value
            row['cod_amount'] += cod
            row['charges'] += charges
            if state == 'delivered':
                row['delivered'] += count
            elif state == 'cancelled':
                row['cancelled'] += count
        status_rows = [
            {'state': key, 'label': label,
             'count': by_state[key]['count'],
             'value': self._round(by_state[key]['value'])}
            for key, label in delivery_states if key in by_state
        ]
        seller_rows = sorted(by_seller.values(), key=lambda r: r['seller'].lower())
        for row in seller_rows:
            for key in ('value', 'cod_amount', 'charges'):
                row[key] = self._round(row[key])
        for key in ('value', 'cod_amount', 'charges'):
            totals[key] = self._round(totals[key])
        return {
            'totals': totals,
            'status_rows': status_rows,
            'seller_rows': seller_rows,
            'domain': domain,
        }

    @api.model
    def order_report_xlsx(self, date_from=None, date_to=None, seller_ids=None,
                          states=None, include_seller_sheet=True):
        data = self.order_report(date_from, date_to, seller_ids, states)
        sellers = self.env['logistics.seller'].browse(seller_ids or [])
        extra = []
        if states:
            extra.append((_('Status'), ', '.join(STATE_LABELS.get(s, s) for s in states)))
        meta = self._filter_meta(
            self._to_date(date_from), self._to_date(date_to), sellers, extra)
        t = data['totals']
        sheets = [{
            'name': _('Summary'),
            'title': _('Order Report'),
            'meta': meta,
            'columns': [(_('Report'), TEXT), (_('Value'), MONEY)],
            'rows': [
                [_('Total Order Count'), t['count']],
                [_('Total Order Value'), t['value']],
                [_('Delivered Orders'), t['delivered']],
                [_('Cancelled Orders'), t['cancelled']],
                [_('COD Amount'), t['cod_amount']],
                [_('Delivery Charges'), t['charges']],
            ],
        }, {
            'name': _('Status Summary'),
            'title': _('Order Status Summary'),
            'meta': meta,
            'columns': [(_('Status'), TEXT), (_('Orders'), INT), (_('Order Value'), MONEY)],
            'rows': [[r['label'], r['count'], r['value']] for r in data['status_rows']],
            'totals': [_('Total'), t['count'], t['value']],
        }]
        if include_seller_sheet:
            sheets.append({
                'name': _('Seller-wise'),
                'title': _('Seller-wise Order Report'),
                'meta': meta,
                'columns': [
                    (_('Seller'), TEXT), (_('Orders'), INT), (_('Order Value'), MONEY),
                    (_('Delivered'), INT), (_('Cancelled'), INT),
                    (_('COD Amount'), MONEY), (_('Delivery Charges'), MONEY),
                ],
                'rows': [
                    [r['seller'], r['count'], r['value'], r['delivered'],
                     r['cancelled'], r['cod_amount'], r['charges']]
                    for r in data['seller_rows']
                ],
                'totals': [_('Total'), t['count'], t['value'], t['delivered'],
                           t['cancelled'], t['cod_amount'], t['charges']],
            })
        shipments = self.env['logistics.shipment'].search(
            data['domain'], order='order_date desc, id desc')
        sheets.append({
            'name': _('Orders'),
            'title': _('Orders'),
            'meta': meta,
            'columns': [
                (_('Order Date'), DATE), (_('AWB / Reference'), TEXT),
                (_('Order'), TEXT), (_('Seller'), TEXT), (_('Customer'), TEXT),
                (_('Status'), TEXT), (_('Payment'), TEXT),
                (_('Order Value'), MONEY), (_('COD Amount'), MONEY),
                (_('Delivery Charges'), MONEY),
            ],
            'rows': [
                [s.order_date, s.display_name, s.order_id.name or '',
                 s.seller_id.name or '', s.shipping_to_name or '',
                 STATE_LABELS.get(s.state, s.state or ''),
                 dict(s._fields['order_payment_type'].selection).get(
                     s.order_payment_type, ''),
                 s.total_order_value, s.cod_amount, s.delivery_charges_total]
                for s in shipments
            ],
        })
        return build_workbook(sheets)

    # ------------------------------------------------------------------
    # COD wallet
    # ------------------------------------------------------------------
    @api.model
    def seller_cod_balance(self, seller, as_of=None):
        """Pending COD for one seller from the COD ledger."""
        domain = [('seller_id', '=', seller.id)]
        as_of = self._to_date(as_of)
        if as_of:
            domain.append(('date', '<=', as_of))
        [(total,)] = self.env['kx.cod.ledger.line']._read_group(domain, [], ['amount:sum'])
        return self._round(total)

    @api.model
    def cod_report(self, date_from=None, date_to=None, seller_ids=None):
        """Total, seller-wise and date-wise COD balances for the filters."""
        Line = self.env['kx.cod.ledger.line']
        date_from, date_to = self._to_date(date_from), self._to_date(date_to)
        base = [('seller_id', 'in', list(seller_ids))] if seller_ids else []
        before = base + ([('date', '<', date_from)] if date_from else [('id', '=', 0)])
        period = list(base)
        if date_from:
            period.append(('date', '>=', date_from))
        if date_to:
            period.append(('date', '<=', date_to))

        openings = {
            seller.id: amount or 0.0
            for seller, amount in Line._read_group(before, ['seller_id'], ['amount:sum'])
        }
        movements = {
            seller.id: (credit or 0.0, debit or 0.0)
            for seller, credit, debit in Line._read_group(
                period, ['seller_id'], ['credit:sum', 'debit:sum'])
        }
        seller_ids_seen = set(openings) | set(movements)
        sellers = self.env['logistics.seller'].browse(sorted(seller_ids_seen)).exists()
        seller_rows = []
        for seller in sellers.sorted(lambda s: (s.name or '').lower()):
            opening = openings.get(seller.id, 0.0)
            credit, debit = movements.get(seller.id, (0.0, 0.0))
            seller_rows.append({
                'seller_id': seller.id,
                'seller': seller.name or '',
                'opening': self._round(opening),
                'credit': self._round(credit),
                'debit': self._round(debit),
                'closing': self._round(opening + credit - debit),
            })

        running = sum(openings.values())
        day_rows = []
        for day, credit, debit in Line._read_group(
                period, ['date:day'], ['credit:sum', 'debit:sum'], order='date:day asc'):
            credit, debit = credit or 0.0, debit or 0.0
            day_rows.append({
                'date': fields.Date.to_date(day),
                'opening': self._round(running),
                'credit': self._round(credit),
                'debit': self._round(debit),
                'closing': self._round(running + credit - debit),
            })
            running += credit - debit
        return {
            'total_balance': self._round(sum(r['closing'] for r in seller_rows)),
            'opening_total': self._round(sum(openings.values())),
            'seller_rows': seller_rows,
            'day_rows': day_rows,
            'period_domain': period,
        }

    @api.model
    def cod_report_xlsx(self, date_from=None, date_to=None, seller_ids=None,
                        include_seller_sheet=True):
        data = self.cod_report(date_from, date_to, seller_ids)
        sellers = self.env['logistics.seller'].browse(seller_ids or [])
        meta = self._filter_meta(self._to_date(date_from), self._to_date(date_to), sellers)
        sheets = []
        if include_seller_sheet:
            sheets.append({
                'name': _('Seller-wise Balance'),
                'title': _('COD Wallet: Seller-wise Balance'),
                'meta': meta + [(_('Total COD balance'), '%.2f' % data['total_balance'])],
                'columns': [
                    (_('Seller'), TEXT), (_('Opening'), MONEY), (_('Credit'), MONEY),
                    (_('Debit'), MONEY), (_('Balance'), MONEY),
                ],
                'rows': [[r['seller'], r['opening'], r['credit'], r['debit'], r['closing']]
                         for r in data['seller_rows']],
                'totals': [
                    _('Total'),
                    sum(r['opening'] for r in data['seller_rows']),
                    sum(r['credit'] for r in data['seller_rows']),
                    sum(r['debit'] for r in data['seller_rows']),
                    data['total_balance'],
                ],
            })
        sheets.append({
            'name': _('Date-wise'),
            'title': _('COD Wallet: Date-wise Balance'),
            'meta': meta,
            'columns': [
                (_('Date'), DATE), (_('Opening'), MONEY), (_('Credit'), MONEY),
                (_('Debit'), MONEY), (_('Closing Balance'), MONEY),
            ],
            'rows': [[r['date'], r['opening'], r['credit'], r['debit'], r['closing']]
                     for r in data['day_rows']],
        })
        lines = self.env['kx.cod.ledger.line'].search(
            data['period_domain'], order='date asc, id asc')
        category_labels = dict(self.env['kx.cod.ledger.line']._fields['category'].selection)
        sheets.append({
            'name': _('Transactions'),
            'title': _('COD Transactions'),
            'meta': meta,
            'columns': [
                (_('Date'), DATE), (_('Seller'), TEXT), (_('Reference'), TEXT),
                (_('Description'), TEXT), (_('Type'), TEXT), (_('Cr/Dr'), TEXT),
                (_('Credit'), MONEY), (_('Debit'), MONEY), (_('Balance'), MONEY),
            ],
            'rows': [
                [l.date, l.seller_id.name or '', l.reference or '', l.description or '',
                 category_labels.get(l.category, ''),
                 _('Credit') if l.amount >= 0 else _('Debit'),
                 l.credit, l.debit, l.balance]
                for l in lines
            ],
        })
        return build_workbook(sheets)

    # ------------------------------------------------------------------
    # Seller ledger
    # ------------------------------------------------------------------
    @api.model
    def ledger_statement(self, seller, date_from=None, date_to=None, source=None):
        """Opening balances, lines and closing balances for one seller."""
        Line = self.env['kx.seller.ledger.line']
        date_from, date_to = self._to_date(date_from), self._to_date(date_to)
        opening_wallet = opening_cod = 0.0
        if date_from:
            [(opening_wallet, opening_cod)] = Line._read_group(
                [('seller_id', '=', seller.id), ('date', '<', date_from)],
                [], ['wallet_amount:sum', 'cod_amount:sum'])
        domain = [('seller_id', '=', seller.id)]
        if date_from:
            domain.append(('date', '>=', date_from))
        if date_to:
            domain.append(('date', '<=', date_to))
        records = Line.search(domain, order='date asc, id asc')
        closing_wallet = (opening_wallet or 0.0) + sum(records.mapped('wallet_amount'))
        closing_cod = (opening_cod or 0.0) + sum(records.mapped('cod_amount'))
        if source:
            records = records.filtered(lambda l: l.source == source)
        source_labels = dict(Line._fields['source'].selection)
        lines = [{
            'date': l.date,
            'reference': l.reference or '',
            'description': l.description or '',
            'source': source_labels.get(l.source, ''),
            'category': CATEGORY_LABELS.get(l.category, ''),
            'entry_type': _('Credit') if l.entry_type == 'credit' else _('Debit'),
            'credit': l.credit,
            'debit': l.debit,
            'amount': l.amount,
            'wallet_balance': l.wallet_balance,
            'cod_balance': l.cod_balance,
        } for l in records]
        return {
            'seller': seller,
            'date_from': date_from,
            'date_to': date_to,
            'source': source,
            'currency': seller.currency_id or self._currency(),
            'opening_wallet': self._round(opening_wallet),
            'opening_cod': self._round(opening_cod),
            'closing_wallet': self._round(closing_wallet),
            'closing_cod': self._round(closing_cod),
            'total_credit': self._round(sum(l['credit'] for l in lines)),
            'total_debit': self._round(sum(l['debit'] for l in lines)),
            'lines': lines,
        }

    @api.model
    def ledger_statement_xlsx(self, sellers, date_from=None, date_to=None, source=None):
        sheets = []
        for seller in sellers:
            data = self.ledger_statement(seller, date_from, date_to, source)
            meta = self._filter_meta(data['date_from'], data['date_to'], seller, [
                (_('Opening wallet balance'), '%.2f' % data['opening_wallet']),
                (_('Opening COD balance'), '%.2f' % data['opening_cod']),
                (_('Closing wallet balance'), '%.2f' % data['closing_wallet']),
                (_('Closing COD balance'), '%.2f' % data['closing_cod']),
            ])
            sheets.append({
                'name': (seller.name or _('Seller'))[:31],
                'title': _('Ledger Statement: %s') % (seller.name or ''),
                'meta': meta,
                'columns': [
                    (_('Date'), DATE), (_('Reference'), TEXT), (_('Description'), TEXT),
                    (_('Source'), TEXT), (_('Type'), TEXT), (_('Cr/Dr'), TEXT),
                    (_('Credit'), MONEY), (_('Debit'), MONEY),
                    (_('Wallet Balance'), MONEY), (_('COD Balance'), MONEY),
                ],
                'rows': [
                    [l['date'], l['reference'], l['description'], l['source'],
                     l['category'], l['entry_type'], l['credit'], l['debit'],
                     l['wallet_balance'], l['cod_balance']]
                    for l in data['lines']
                ],
                'totals': ['', _('Total'), '', '', '', '', data['total_credit'],
                           data['total_debit'], data['closing_wallet'], data['closing_cod']],
            })
        if not sheets:
            sheets.append({'name': _('Ledger'), 'title': _('No seller selected')})
        return build_workbook(sheets)
