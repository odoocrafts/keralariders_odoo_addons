import io

import xlsxwriter

# Column kinds understood by ``build_workbook``.
TEXT, INT, MONEY, DATE = 'text', 'int', 'money', 'date'


def build_workbook(sheets):
    """Render a list of sheet specs into xlsx bytes.

    Each sheet is a dict:

    - ``name``: worksheet tab name (Excel caps it at 31 characters)
    - ``title``: heading on the first row
    - ``meta``: list of ``(label, value)`` lines printed under the title,
      usually the filters the report was run with
    - ``columns``: list of ``(header, kind)``; kind is one of TEXT, INT,
      MONEY or DATE
    - ``rows``: list of row value lists, in column order
    - ``totals``: optional row printed in bold after the data
    """
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {'in_memory': True})
    title_fmt = workbook.add_format({'bold': True, 'font_size': 14})
    meta_fmt = workbook.add_format({'italic': True, 'font_color': '#555555'})
    head_fmt = workbook.add_format({
        'bold': True, 'bg_color': '#1F3864', 'font_color': '#FFFFFF',
        'border': 1, 'text_wrap': True, 'valign': 'vcenter',
    })
    formats = {
        TEXT: workbook.add_format({'border': 1}),
        INT: workbook.add_format({'border': 1, 'num_format': '0'}),
        MONEY: workbook.add_format({'border': 1, 'num_format': '#,##0.00'}),
        DATE: workbook.add_format({'border': 1, 'num_format': 'dd-mm-yyyy'}),
    }
    total_formats = {
        kind: workbook.add_format({
            'border': 1, 'bold': True, 'bg_color': '#D9E1F2',
            'num_format': fmt,
        })
        for kind, fmt in (
            (TEXT, '@'), (INT, '0'), (MONEY, '#,##0.00'), (DATE, 'dd-mm-yyyy'),
        )
    }
    for sheet in sheets:
        ws = workbook.add_worksheet((sheet.get('name') or 'Report')[:31])
        row = 0
        ws.write(row, 0, sheet.get('title') or '', title_fmt)
        row += 1
        for label, value in sheet.get('meta') or []:
            ws.write(row, 0, '%s: %s' % (label, value), meta_fmt)
            row += 1
        row += 1
        columns = sheet.get('columns') or []
        for col, (header, kind) in enumerate(columns):
            ws.write(row, col, header, head_fmt)
            ws.set_column(col, col, 14 if kind != TEXT else 24)
        header_row = row
        row += 1
        for values in sheet.get('rows') or []:
            _write_row(ws, row, columns, values, formats)
            row += 1
        if sheet.get('totals'):
            _write_row(ws, row, columns, sheet['totals'], total_formats)
        if columns:
            ws.freeze_panes(header_row + 1, 0)
    workbook.close()
    return output.getvalue()


def _write_row(ws, row, columns, values, formats):
    for col, (_header, kind) in enumerate(columns):
        value = values[col] if col < len(values) else None
        fmt = formats[kind]
        if value is None or value == '':
            ws.write_blank(row, col, None, fmt)
        elif kind == DATE:
            ws.write_datetime(row, col, _as_datetime(value), fmt)
        elif kind in (INT, MONEY):
            ws.write_number(row, col, float(value), fmt)
        else:
            ws.write_string(row, col, str(value), fmt)


def _as_datetime(value):
    from datetime import date, datetime
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return value
