"""Parse a seller's filled WhatsApp order message into portal form fields.

The copy button on Add Order and bulk upload pastes ``WHATSAPP_ORDER_TEMPLATE``.
This parser accepts that text, including small label and colon variations.
Keep ``static/src/js/portal_whatsapp_order.js`` in step with the aliases,
separators, and gram-to-kilogram conversion here. Bulk create uses this
module so the rules are the ones the tests run.
"""

import re

# Exact text the portal "Copy WhatsApp template" button copies.
# Weight is grams. The portal form stores kilograms (500 g -> 0.5 kg).
WHATSAPP_ORDER_TEMPLATE = (
    "Name: Customer Name\n"
    "Mobile: 9800000000\n"
    "Address: House name, Street, Area\n"
    "Pincode: 682001\n"
    "Weight g: 500\n"
    "Length cm: 10\n"
    "Breadth cm: 10\n"
    "Height cm: 10\n"
    "Payment: Prepaid\n"
    "COD amount: 0\n"
    "Item: Sample item"
)

# How many pasted messages one bulk create will accept.
WHATSAPP_ORDER_LIMIT = 100

_LINE_RE = re.compile(r'^\s*(.+?)\s*[:\-\u2013\u2014]\s*(.*)\s*$')
_NUMBER_RE = re.compile(r'\d+(?:\.\d+)?')

# Normalized label -> internal key. The template labels are the first alias
# of each field. Extra aliases cover case, spacing, and CSV-style names.
_ALIASES = {
    'name': 'name',
    'customer name': 'name',
    'receiver name': 'name',
    'receiver': 'name',
    'mobile': 'mobile',
    'mobile number': 'mobile',
    'phone': 'mobile',
    'phone number': 'mobile',
    'contact': 'mobile',
    'address': 'address',
    'address line': 'address',
    'shipping address': 'address',
    'pincode': 'pincode',
    'pin code': 'pincode',
    'pin': 'pincode',
    'zip': 'pincode',
    'zipcode': 'pincode',
    'zip code': 'pincode',
    'weight g': 'weight_g',
    'weight grams': 'weight_g',
    'weight gram': 'weight_g',
    'weight': 'weight_g',
    'wt': 'weight_g',
    'length cm': 'length_cm',
    'length': 'length_cm',
    'breadth cm': 'breadth_cm',
    'breadth': 'breadth_cm',
    'width cm': 'breadth_cm',
    'width': 'breadth_cm',
    'height cm': 'height_cm',
    'height': 'height_cm',
    'payment': 'payment',
    'payment type': 'payment',
    'cod amount': 'cod_amount',
    'cod amt': 'cod_amount',
    'order value': 'cod_amount',
    'total order value': 'cod_amount',
    'item': 'item',
    'item description': 'item',
    'description': 'item',
    'contents': 'item',
    'state': 'state',
    'service': 'service',
    'india post service': 'service',
}

_REQUIRED = (
    ('name', 'Name'),
    ('mobile', 'Mobile'),
    ('address', 'Address'),
    ('pincode', 'Pincode'),
    ('weight_g', 'Weight'),
    ('item', 'Item'),
)

_PREPAID = {'prepaid', 'pre paid', 'paid', 'online', 'already paid'}
_COD = {'cod', 'cash on delivery', 'cash'}


def parse_whatsapp_orders(text):
    """Split ``text`` into order blocks and parse each one.

    Blocks are separated by a blank line or a line that is only ``---``.
    A failed block is reported and does not remove the blocks that parsed.
    Returns a list of dicts: ``index`` (1-based), ``ok``, ``reason``, ``post``.
    ``post`` uses the portal form field names when ``ok`` is true.
    """
    return [
        _parse_block(index, block)
        for index, block in enumerate(_split_blocks(text), start=1)
    ]


def _split_blocks(text):
    normalized = (text or '').replace('\ufeff', '')
    normalized = normalized.replace('\r\n', '\n').replace('\r', '\n')
    normalized = normalized.replace('\u2028', '\n').replace('\u2029', '\n')
    blocks = []
    current = []
    for line in normalized.split('\n'):
        if not line.strip() or line.strip() == '---':
            if current:
                blocks.append('\n'.join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append('\n'.join(current))
    return blocks


def _parse_block(index, block):
    values = _read_fields(block)
    if not values:
        return _failure(
            index,
            'Could not read this message. Use the template: one field per line.',
        )

    payment, payment_error = _payment(values.get('payment'))
    weight = _weight_kg(values.get('weight_g'))
    mobile = _mobile(values.get('mobile'))
    pincode = _pincode(values.get('pincode'))
    cod_amount = _parse_money(values.get('cod_amount')) if 'cod_amount' in values else None

    missing = []
    if not (values.get('name') or '').strip():
        missing.append('Name')
    if not mobile:
        missing.append('Mobile')
    if not (values.get('address') or '').strip():
        missing.append('Address')
    if not pincode:
        missing.append('Pincode')
    if not weight:
        missing.append('Weight')
    if not (values.get('item') or '').strip():
        missing.append('Item')
    if payment == 'cod' and cod_amount is None:
        missing.append('COD amount')

    post = {'order_payment_type': payment}
    if (values.get('name') or '').strip():
        post['shipping_to_name'] = values['name'].strip()
    if mobile:
        post['shipping_to_mobile'] = mobile
    if (values.get('address') or '').strip():
        post['shipping_to_address'] = values['address'].strip()
    if pincode:
        post['shipping_to_zip'] = pincode
    if weight:
        post['total_weight'] = weight
    if (values.get('item') or '').strip():
        post['item_description'] = values['item'].strip()
    for source, target in (
        ('length_cm', 'length_cm'),
        ('breadth_cm', 'breadth_cm'),
        ('height_cm', 'height_cm'),
    ):
        measure = _measure(values.get(source))
        if measure:
            post[target] = measure
    if cod_amount is not None:
        post['total_order_value'] = _format_number(cod_amount, 2)
    elif payment == 'prepaid':
        post['total_order_value'] = '0'
    state_name = (values.get('state') or '').strip()
    if state_name:
        post['shipping_to_state_name'] = state_name
    service = _service(values.get('service'))
    if service:
        post['indiapost_article_type'] = service

    reasons = []
    if missing:
        weight_raw = (values.get('weight_g') or '').strip()
        labels = list(missing)
        if weight_raw and not weight and 'Weight' in labels:
            labels.remove('Weight')
            if labels:
                reasons.append('Missing %s' % ', '.join(labels))
            reasons.append('Weight must be greater than 0.')
        else:
            reasons.append('Missing %s' % ', '.join(labels))
    if payment_error:
        reasons.append(payment_error)
    if reasons:
        return {
            'index': index,
            'ok': False,
            'reason': ' '.join(reasons),
            'post': post,
        }
    return {
        'index': index,
        'ok': True,
        'reason': '',
        'post': post,
    }


def _failure(index, reason):
    return {'index': index, 'ok': False, 'reason': reason, 'post': {}}


def _read_fields(block):
    values = {}
    last_key = None
    for line in block.split('\n'):
        match = _LINE_RE.match(line)
        key = _ALIASES.get(_norm_label(match.group(1))) if match else None
        if key:
            values[key] = (match.group(2) or '').strip()
            last_key = key
            continue
        # A wrapped address (or any continuation) stays on the previous field.
        # Lines before the first known label are a greeting and are ignored.
        if last_key and line.strip():
            values[last_key] = ('%s %s' % (values[last_key], line.strip())).strip()
    return values


def _norm_label(label):
    text = (label or '').strip().lower().replace('_', ' ')
    text = text.replace('(', ' ').replace(')', ' ').replace('*', ' ')
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _payment(raw):
    if raw is None or not str(raw).strip():
        return 'prepaid', ''
    text = _norm_label(raw)
    if text in _COD or text.startswith('cod'):
        return 'cod', ''
    if text in _PREPAID or 'prepaid' in text or 'pre paid' in text:
        return 'prepaid', ''
    return 'prepaid', 'Payment must be Prepaid or COD.'


def _service(raw):
    text = _norm_label(raw or '')
    if not text:
        return ''
    if text in ('sp', 'speed post') or 'speed' in text:
        return 'SP'
    if text in ('bp', 'business parcel', 'normal parcel') or 'business' in text or 'parcel' in text:
        return 'BP'
    return ''


def _weight_kg(raw):
    """Template weight is grams. A value that says kg is already kilograms."""
    text = (raw or '').strip().lower().replace(',', '')
    if not text:
        return ''
    match = _NUMBER_RE.search(text)
    if not match:
        return ''
    number = float(match.group(0))
    kg = number if 'kg' in text else number / 1000.0
    kg = round(kg, 3)
    if kg <= 0:
        return ''
    return _format_number(kg, 3)


def _measure(raw):
    text = (raw or '').strip().lower().replace(',', '')
    if not text:
        return ''
    match = _NUMBER_RE.search(text)
    if not match:
        return ''
    number = round(float(match.group(0)), 3)
    if number <= 0:
        return ''
    return _format_number(number, 3)


def _parse_money(raw):
    text = (raw or '').strip().replace(',', '')
    if text == '':
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    return float(match.group(0))


def _format_number(number, places):
    text = ('%.*f' % (places, number)).rstrip('0').rstrip('.')
    return text or '0'


def _mobile(raw):
    text = (raw or '').strip()
    digits = re.sub(r'\D', '', text)
    if not digits:
        return ''
    if text.startswith('+'):
        return '+' + digits
    return digits


def _pincode(raw):
    return re.sub(r'\D', '', raw or '')
