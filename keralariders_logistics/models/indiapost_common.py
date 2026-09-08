"""Constants and pure helpers shared by the India Post integration.

Nothing in this module touches the ORM, so the same rules can be exercised from
the standalone probe scripts in ``refs/`` against the live sandbox.

Every limit here was checked against https://test.cept.gov.in/beextcustomer.
Where the live API disagrees with the vendor document, the comment says so and
the live behaviour wins.
"""

import datetime
import math
import re

# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
# India Post recognises exactly these two article types, verified live:
# product-code=BP prices as BUSINESS_PARCEL, while PP and every other guess is
# refused with "article_type 'PP' is not a recognized article type".
ARTICLE_TYPE_SPEED_POST = 'SP'
ARTICLE_TYPE_BUSINESS_PARCEL = 'BP'

ARTICLE_TYPES = [
    (ARTICLE_TYPE_SPEED_POST, 'Speed Post'),
    (ARTICLE_TYPE_BUSINESS_PARCEL, 'Business Parcel'),
]
ARTICLE_TYPE_LABELS = dict(ARTICLE_TYPES)

# A bulk customer is contracted per service, so India Post issued KeralaXpress
# one contract number per product and a booking has to carry the one belonging
# to its own product. This is the mapping from product to settings field.
CONTRACT_SETTING_BY_ARTICLE_TYPE = {
    ARTICLE_TYPE_SPEED_POST: 'indiapost_sp_contract_id',
    ARTICLE_TYPE_BUSINESS_PARCEL: 'indiapost_bp_contract_id',
}

# Inbound webhook paths registered on India Post's Customer Self-Service
# Portal. India Post POSTs to us; we never call these URLs. Paths are fixed
# because they are already live on production.
BOOKING_WEBHOOK_PATH = '/indiapost/bookingeventwebhook'
OTHER_WEBHOOK_PATH = '/indiapost/othereventwebhook'

# Within Speed Post the API picks one of these two concrete products from the
# weight and returns the name in the tariff response.
PRODUCT_DOC = 'SP_INLAND_DOC'
PRODUCT_PARCEL = 'SP_INLAND_PARCEL'

# The API picks the product purely from the physical weight. 500 g exactly is
# still billed as a document (verified live: 500 g at 14 x 9 x 1 came back as
# SP_INLAND_DOC), so the parcel rules only start above 500 g.
DOC_WEIGHT_MAX_G = 500

# shape_of_article: DOC for documents, ROLL for cylindrical parcels and NROL
# for rectangular ones.
SHAPE_DOC = 'DOC'
SHAPE_CYLINDRICAL = 'ROLL'
SHAPE_RECTANGULAR = 'NROL'

# Dimensions are cm, weights grams. Ranges are inclusive.
PRODUCT_LIMITS = {
    PRODUCT_DOC: {
        'weight': (1, 500),
        'length': (1, 42),
        'breadth': (1, 29),
        'height': (1, 2),
    },
    PRODUCT_PARCEL: {
        'weight': (1, 35000),
        'length': (14, 150),
        'breadth': (9, 150),
        'height': (1, 150),
    },
}

# Undocumented but enforced with HTTP 422: length + breadth + height <= 300 cm.
MAX_TOTAL_DIMENSION_CM = 300

# Verified live: 30 x 20 x 15 cm at 1500 g is billed at 1800 g, i.e. cm^3 / 5.
VOLUMETRIC_DIVISOR = 5

# Postal weight steps. Quotes are banded up to the next step so the tariff
# cache gets useful hit rates on the public calculator without ever
# under-quoting a seller.
QUOTE_WEIGHT_BAND_G = 50

# ---------------------------------------------------------------------------
# Pickup scheduling
# ---------------------------------------------------------------------------
# Only these two slots are accepted; anything else is rejected with
# "Pickup schedule slot must be one of 10:00-13:00, 13:00-16:00".
PICKUP_SLOTS = [
    ('10:00-13:00', '10:00 AM - 1:00 PM'),
    ('13:00-16:00', '1:00 PM - 4:00 PM'),
]
PICKUP_SLOT_START_HOUR = {'10:00-13:00': 10, '13:00-16:00': 13}

# pickup_schedule_date is format-checked ("Invalid pickup schedule date format.
# Expected format: MM/DD/YYYY HH:MM:SS AM/PM") but not sanity-checked: a date in
# 2020 was accepted without complaint, and so was an ISO 8601 string. We emit
# exactly the documented format and validate future-dating ourselves.
PICKUP_DATETIME_FORMAT = '%m/%d/%Y %I:%M:%S %p'

# ---------------------------------------------------------------------------
# Field length / format rules
# ---------------------------------------------------------------------------
# The document specifies 3-80 characters for names, company, address lines,
# city, state and email, 10-digit mobiles starting 6-9, and a 240 character
# cap across all address lines.
#
# Of those, the live validator only enforces the 80-character maximum and the
# 6-digit pincode. A 2-character city, a 9-digit mobile starting with 5, an
# empty receiver address line and address lines totalling 300 characters were
# all accepted for booking. That is worse than rejection: the article books and
# then fails physically, so we enforce the documented rules ourselves.
TEXT_MIN_LEN = 3
TEXT_MAX_LEN = 80
ADDRESS_LINES_MAX_TOTAL = 240
ADDRESS_LINE_COUNT = 3
MOBILE_RE = re.compile(r'^[6-9]\d{9}$')
PINCODE_RE = re.compile(r'^\d{6}$')
BULK_CUSTOMER_ID_RE = re.compile(r'^\d{10}$')
CONTRACT_ID_RE = re.compile(r'^\d{8}$')
BULK_REFERENCE_MAX_LEN = 50

# ---------------------------------------------------------------------------
# Barcode / AWB
# ---------------------------------------------------------------------------
# 13 characters: 2 prefix + 8 serial + 1 check digit + "IN".
BARCODE_SUFFIX = 'IN'
BARCODE_SERIAL_DIGITS = 8
# Weighted modulo 11, factors applied left to right across the serial.
BARCODE_WEIGHTS = (8, 6, 4, 2, 3, 5, 9, 7)
BARCODE_RE = re.compile(r'^[A-Z]{2}\d{9}[A-Z]{2}$')

# ---------------------------------------------------------------------------
# Office selection
# ---------------------------------------------------------------------------
# Bookable means delivery_office_flag is true and the office is not a Branch
# Post Office.
NON_BOOKABLE_OFFICE_TYPES = {'BPO'}

# Preference order when a pincode resolves to several bookable offices. Head
# and General Post Offices first, then sub offices, then the Intra-city
# Delivery Centres, which are extremely common in Kerala.
OFFICE_TYPE_PREFERENCE = {'GPO': 0, 'HPO': 0, 'SPO': 1, 'MDG': 2, 'SO': 2, 'IDC': 3}
OFFICE_TYPE_PREFERENCE_DEFAULT = 9


class IndiapostDataError(ValueError):
    """A shipment or configuration value cannot be expressed to India Post.

    Raised by the helpers below and translated into a Odoo ValidationError by
    the callers, which is why this module stays ORM free.
    """


# ---------------------------------------------------------------------------
# Barcode helpers
# ---------------------------------------------------------------------------
def barcode_check_digit(serial):
    """Weighted modulo 11 check digit for an 8-digit serial.

    Verified against the vendor's worked example: serial 47312482 -> 9.
    """
    digits = '%0*d' % (BARCODE_SERIAL_DIGITS, int(serial))
    if len(digits) != BARCODE_SERIAL_DIGITS:
        raise IndiapostDataError(
            'Barcode serial %s does not fit in %d digits.'
            % (serial, BARCODE_SERIAL_DIGITS)
        )
    remainder = sum(int(d) * w for d, w in zip(digits, BARCODE_WEIGHTS)) % 11
    if remainder == 0:
        return '5'
    if remainder == 1:
        return '0'
    return str(11 - remainder)


def build_barcode(prefix, serial):
    """Assemble the 13-character article barcode for a serial."""
    prefix = (prefix or '').strip().upper()
    if len(prefix) != 2 or not prefix.isalpha():
        raise IndiapostDataError(
            'Barcode prefix must be exactly two letters, got %r.' % prefix
        )
    return '%s%0*d%s%s' % (
        prefix, BARCODE_SERIAL_DIGITS, int(serial),
        barcode_check_digit(serial), BARCODE_SUFFIX,
    )


def barcode_is_wellformed(barcode):
    """True when the barcode has the right shape and a matching check digit."""
    barcode = (barcode or '').strip().upper()
    if not BARCODE_RE.match(barcode):
        return False
    serial = barcode[2:10]
    return barcode[10] == barcode_check_digit(serial)


# ---------------------------------------------------------------------------
# Weight and dimensions
# ---------------------------------------------------------------------------
def kg_to_grams(weight_kg):
    """Convert a Kg float to whole grams, rounding up, never below 1.

    The tariff and booking endpoints reject decimal weights, and a 0 g article
    is meaningless, so both edges are clamped here rather than at each caller.
    """
    try:
        value = float(weight_kg or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    # The epsilon keeps 1.5 kg at 1500 g instead of 1501 g when the stored
    # float is a hair above the decimal value.
    grams = int(math.ceil(value * 1000.0 - 1e-6))
    return max(grams, 1)


def cm_to_int(value):
    """Round a cm dimension up to the whole centimetre the API expects."""
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        number = 0.0
    return max(int(math.ceil(number - 1e-6)), 0)


def band_weight(grams):
    """Round grams up to the next postal weight step, for quote caching."""
    grams = max(int(grams or 0), 1)
    return int(math.ceil(grams / float(QUOTE_WEIGHT_BAND_G))) * QUOTE_WEIGHT_BAND_G


def volumetric_weight_g(length_cm, breadth_cm, height_cm):
    """Volumetric weight in grams: L x B x H in cm divided by 5."""
    length, breadth, height = (cm_to_int(length_cm), cm_to_int(breadth_cm),
                               cm_to_int(height_cm))
    if not (length and breadth and height):
        return 0
    return int(math.ceil(length * breadth * height / float(VOLUMETRIC_DIVISOR)))


def chargeable_weight_g(actual_g, length_cm, breadth_cm, height_cm):
    """The weight India Post bills.

    Volumetric weight applies to parcels only. Verified against the live tariff
    endpoint: a 250 g document measuring 40 x 29 x 2 cm has a volumetric weight
    of 464 g but was quoted a chargeable weight of 250 g, and the same held at
    100 g and 499 g. Cross the 500 g line and the volumetric rule kicks in, as
    it did for 600 g at 30 x 20 x 15 cm (chargeable 1800 g).
    """
    actual = int(actual_g or 0)
    if resolve_product_code(actual) == PRODUCT_DOC:
        return actual
    return max(actual, volumetric_weight_g(length_cm, breadth_cm, height_cm))


def resolve_product_code(grams):
    """The concrete Speed Post product the API will bill this weight as."""
    return PRODUCT_PARCEL if int(grams or 0) > DOC_WEIGHT_MAX_G else PRODUCT_DOC


def resolve_shape(grams, cylindrical=False):
    """shape_of_article for a weight, given whether the package is a cylinder."""
    if resolve_product_code(grams) == PRODUCT_DOC:
        return SHAPE_DOC
    return SHAPE_CYLINDRICAL if cylindrical else SHAPE_RECTANGULAR


def validate_package(grams, length_cm, breadth_cm, height_cm):
    """Check a package against the real Speed Post limits.

    Returns ``(errors, warnings)``, both lists of human-readable sentences.
    Errors mean the API will refuse the article. Warnings mean the vendor
    document forbids it but the live sandbox accepted it, so we surface the
    risk without blocking a booking that would in fact succeed.

    This is the only guard that exists. The booking endpoint validates weight
    but not dimensions at all: a 600 g article at 10 x 5 x 5 cm and a package
    totalling 400 cm were both accepted for booking, even though the tariff
    endpoint rejects them with HTTP 422. Without these checks a shipment would
    book successfully and then be refused at the counter.
    """
    errors = []
    warnings = []
    grams = int(grams or 0)
    length = cm_to_int(length_cm)
    breadth = cm_to_int(breadth_cm)
    height = cm_to_int(height_cm)

    if grams < 1:
        errors.append('Weight must be at least 1 g.')
    if not (length and breadth and height):
        errors.append(
            'Length, breadth and height are all required for India Post '
            'shipments. Measure the packed article in centimetres.'
        )
        return errors, warnings

    product = resolve_product_code(grams)
    limits = PRODUCT_LIMITS[product]
    weight_min, weight_max = limits['weight']
    if grams > weight_max:
        errors.append(
            'Speed Post accepts up to %.0f kg per article; this one is %.3f kg.'
            % (weight_max / 1000.0, grams / 1000.0)
        )

    total = length + breadth + height
    if total > MAX_TOTAL_DIMENSION_CM:
        errors.append(
            'Length + breadth + height must not exceed %d cm; this package '
            'totals %d cm (%d + %d + %d).'
            % (MAX_TOTAL_DIMENSION_CM, total, length, breadth, height)
        )

    if product == PRODUCT_PARCEL:
        min_l, max_l = limits['length']
        min_b, max_b = limits['breadth']
        min_h, max_h = limits['height']
        # The one that bites sellers: a small dense article at or above 500 g
        # simply cannot travel by Speed Post.
        if length < min_l or breadth < min_b:
            errors.append(
                'An article over %.0f g is billed as a Speed Post parcel, and '
                'parcels must measure at least %d cm x %d cm. This package is '
                '%d cm x %d cm, so India Post cannot carry it. Repack it in a '
                'box of at least %d x %d x 1 cm and re-measure.'
                % (DOC_WEIGHT_MAX_G, min_l, min_b, length, breadth, min_l, min_b)
            )
        if length > max_l or breadth > max_b or height > max_h:
            errors.append(
                'Speed Post parcels must fit within %d x %d x %d cm; this '
                'package is %d x %d x %d cm.'
                % (max_l, max_b, max_h, length, breadth, height)
            )
        # The tariff endpoint bills on volumetric weight but only range-checks
        # the physical one: 150 x 100 x 50 cm quoted a chargeable weight of
        # 150 kg without complaint. That parcel will not survive the counter.
        volumetric = volumetric_weight_g(length, breadth, height)
        if volumetric > weight_max:
            warnings.append(
                'The volumetric weight of this package is %.1f kg, above the '
                '%.0f kg Speed Post ceiling. India Post will quote it, but the '
                'booking office is likely to refuse it. Consider splitting the '
                'consignment.'
                % (volumetric / 1000.0, weight_max / 1000.0)
            )
    else:
        max_l = limits['length'][1]
        max_b = limits['breadth'][1]
        max_h = limits['height'][1]
        if length > max_l or breadth > max_b or height > max_h:
            # The tariff endpoint happily quoted 250 g at 30 x 21 x 5 cm and
            # even at 100 x 80 x 10 cm, so this is not a hard error, but the
            # counter clerk works to the document, not to the API.
            warnings.append(
                'Articles up to %.0f g are billed as documents, which are '
                'meant to fit within %d x %d x %d cm. This package is '
                '%d x %d x %d cm. India Post quoted it anyway, but the '
                'booking office may refuse it — either use a flat envelope or '
                'check that the weight is really under %.0f g.'
                % (DOC_WEIGHT_MAX_G, max_l, max_b, max_h,
                   length, breadth, height, DOC_WEIGHT_MAX_G)
            )
    return errors, warnings


# ---------------------------------------------------------------------------
# Text / contact normalisation
# ---------------------------------------------------------------------------
def normalize_pincode(value, label='Pincode'):
    """Exactly six digits.

    Worth being strict: a 5-digit pincode makes pincode-search do a prefix
    match and return 16 unrelated offices instead of erroring.
    """
    pincode = re.sub(r'\D', '', str(value or ''))
    if not PINCODE_RE.match(pincode):
        raise IndiapostDataError(
            '%s must be exactly 6 digits (got %r).' % (label, value or '')
        )
    return pincode


def normalize_mobile(value, label='Mobile number'):
    """Ten digits starting 6-9, tolerating +91 / 0 prefixes and separators."""
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) > 10:
        # Drop a country code or trunk prefix, e.g. +91 98470 12345.
        digits = digits[-10:]
    if not MOBILE_RE.match(digits):
        raise IndiapostDataError(
            '%s must be a 10-digit Indian mobile number starting with 6, 7, 8 '
            'or 9 (got %r).' % (label, value or '')
        )
    return digits


def normalize_text(value, label, min_len=TEXT_MIN_LEN, max_len=TEXT_MAX_LEN,
                   required=True):
    """Collapse whitespace and enforce the 3-80 character rule.

    Deliberately raises instead of padding: a silently padded city is a
    delivery failure nobody can explain three weeks later.
    """
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if not text:
        if required:
            raise IndiapostDataError('%s is required for India Post booking.' % label)
        return ''
    if len(text) < min_len:
        raise IndiapostDataError(
            '%s must be at least %d characters for India Post booking '
            '(got %r). Please expand it.' % (label, min_len, text)
        )
    return text[:max_len]


def split_address_lines(value, label='Address', count=ADDRESS_LINE_COUNT,
                        line_max=TEXT_MAX_LEN, total_max=ADDRESS_LINES_MAX_TOTAL):
    """Turn a free-text address into up to three API address lines.

    Splits on existing newlines first, then wraps on commas or spaces so words
    are not cut in half. The first line must satisfy the 3-character minimum
    because it is the only mandatory one.
    """
    raw = re.sub(r'[ \t]+', ' ', str(value or '')).strip()
    if not raw:
        raise IndiapostDataError('%s is required for India Post booking.' % label)

    chunks = [c.strip(' ,') for c in re.split(r'[\r\n]+', raw) if c.strip(' ,')]
    lines = []
    for chunk in chunks:
        while chunk and len(lines) < count:
            if len(chunk) <= line_max:
                lines.append(chunk)
                chunk = ''
                break
            window = chunk[:line_max + 1]
            cut = max(window.rfind(', '), window.rfind(' '))
            if cut <= 0:
                cut = line_max
            lines.append(chunk[:cut].strip(' ,'))
            chunk = chunk[cut:].strip(' ,')
        if len(lines) >= count:
            break

    lines = [line for line in lines if line]
    if not lines:
        raise IndiapostDataError('%s is required for India Post booking.' % label)
    if len(lines[0]) < TEXT_MIN_LEN:
        raise IndiapostDataError(
            'The first line of %s must be at least %d characters (got %r).'
            % (label, TEXT_MIN_LEN, lines[0])
        )

    # Trim from the tail until the combined length fits the 240 character cap.
    while sum(len(line) for line in lines) > total_max:
        if len(lines) > 1:
            lines.pop()
        else:
            lines[0] = lines[0][:total_max]
    return (lines + [''] * count)[:count]


def format_pickup_datetime(value):
    """Render a date or datetime as MM/DD/YYYY hh:mm:ss AM/PM."""
    if isinstance(value, datetime.datetime):
        moment = value
    elif isinstance(value, datetime.date):
        moment = datetime.datetime.combine(value, datetime.time(10, 0, 0))
    else:
        raise IndiapostDataError('Pickup date %r is not a date.' % (value,))
    return moment.strftime(PICKUP_DATETIME_FORMAT)


def pickup_datetime(pickup_date, slot):
    """Combine a pickup date with the start of its slot."""
    hour = PICKUP_SLOT_START_HOUR.get(slot)
    if hour is None:
        raise IndiapostDataError(
            'Pickup slot must be one of %s.'
            % ', '.join(code for code, _label in PICKUP_SLOTS)
        )
    if isinstance(pickup_date, datetime.datetime):
        pickup_date = pickup_date.date()
    if not isinstance(pickup_date, datetime.date):
        raise IndiapostDataError('A pickup date is required for India Post booking.')
    return datetime.datetime.combine(pickup_date, datetime.time(hour, 0, 0))


# ---------------------------------------------------------------------------
# Response coercion
# ---------------------------------------------------------------------------
def as_amount(value, default=0.0):
    """Coerce a currency figure, tolerating the fractional values the API sends.

    A REG + ACK + OTP combination totalled 109.5 with OTPVAL at 1.5, so nothing
    here may assume integers.
    """
    if value in (None, '', False):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_distance(value):
    """distance_km is usually an integer but comes back as the string "OS"."""
    if value in (None, '', False):
        return None, ''
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text), text
        except ValueError:
            # "OS" (out of station) and anything else non-numeric.
            return None, text
    try:
        return float(value), str(value)
    except (TypeError, ValueError):
        return None, str(value)


def unwrap_records(payload):
    """Pull the record list out of a response.

    The vendor document shows pincode-search returning a bare array; the live
    sandbox wraps it in an envelope. Accept either.
    """
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    if isinstance(payload, dict):
        data = payload.get('data')
        if isinstance(data, list):
            return [record for record in data if isinstance(record, dict)]
        if isinstance(data, dict):
            return [data]
    return []


def office_is_bookable(record):
    """Bookable office rule: a delivery office that is not a Branch Post Office."""
    if not record.get('delivery_office_flag'):
        return False
    return (record.get('office_type_code') or '').upper() not in NON_BOOKABLE_OFFICE_TYPES


def office_sort_key(record):
    """Deterministic ranking so a multi-office pincode always resolves the same.

    Delhi 110001 returns 22 offices, so "just take the first" would silently
    change the booking office whenever the API reorders its result.
    """
    return (
        0 if record.get('is_rolled_out') else 1,
        OFFICE_TYPE_PREFERENCE.get(
            (record.get('office_type_code') or '').upper(),
            OFFICE_TYPE_PREFERENCE_DEFAULT,
        ),
        str(record.get('office_name') or ''),
        str(record.get('office_id') or ''),
    )
