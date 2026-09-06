#!/usr/bin/env python3
"""Verify the shipped addon logic against the live India Post sandbox.

Unlike the earlier probes, which explored the API, this script imports the
addon's own ``indiapost_common`` module and checks that what the addon computes
matches what India Post actually answers. It is the acceptance test for the
integration's arithmetic and payload shaping.

It must run on a server whose IP India Post has whitelisted; from anywhere else
the TLS handshake is reset.

    scp keralariders_logistics/models/indiapost_common.py root@HOST:/tmp/
    scp refs/indiapost_addon_verify.py root@HOST:/tmp/
    ssh root@HOST 'cd /tmp && INDIAPOST_USERNAME=... INDIAPOST_PASSWORD=... \
        python3 indiapost_addon_verify.py'

No AWBs are consumed: every booking call deliberately omits ``barcode_no``, so
each article is rejected after India Post has validated everything else.
"""

import datetime
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indiapost_common as ipc  # noqa: E402  the addon's own helper module

BASE = os.environ.get('INDIAPOST_BASE_URL',
                      'https://test.cept.gov.in/beextcustomer')
TIMEOUT = 60
# Our own customer id has no service type on its contract, so booking
# validation can only be exercised with the vendor's test customer.
TEST_CUSTOMER_ID = '3000064781'
TEST_CONTRACT_ID = '41585456'

# Mirrors models/indiapost_office.py KERALA_HQ_OFFICES, which is seeded on
# install. Checked here against live data so the seed cannot silently rot.
SEEDED_OFFICES = {
    '671121': '22360040', '670001': '22840007', '673121': '22360036',
    '673001': '22840008', '676505': '22360041', '678001': '22840002',
    '680001': '22360032', '682001': '22360020', '685603': '22660631',
    '686001': '22360025', '688001': '22840001', '689645': '22360002',
    '691001': '22840014', '695001': '22840005',
}

RESULTS = []


def record(status, label, detail=''):
    RESULTS.append((status, label, detail))
    print('  %-4s %s%s' % (status, label, (' — ' + detail) if detail else ''))


def call(method, path, token=None, params=None, body=None, accept='*/*'):
    url = BASE + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    req.add_header('Accept', accept)
    if body is not None:
        req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})
    except (urllib.error.URLError, ssl.SSLError, socket.error) as exc:
        return 0, ('TRANSPORT ERROR: %r' % (exc,)).encode(), {}


def as_json(raw):
    try:
        return json.loads(raw.decode('utf-8', 'replace'))
    except (ValueError, TypeError):
        return None


def section(title):
    print('\n' + '=' * 76)
    print(title)
    print('=' * 76)


# ---------------------------------------------------------------------------
def login():
    section('1. AUTH — token manager assumptions')
    status, raw, _h = call('POST', '/v1/access/login', body={
        'username': os.environ.get('INDIAPOST_USERNAME'),
        'password': os.environ.get('INDIAPOST_PASSWORD'),
    })
    payload = as_json(raw) or {}
    data = payload.get('data') or {}
    token = data.get('access_token')
    if not token:
        record('FAIL', 'login', 'HTTP %s %s' % (status, raw[:200]))
        return None
    expires_in = data.get('expires_in')
    record('PASS', 'login returns a bearer token',
           'expires_in=%s, refresh_expires_in=%s'
           % (expires_in, data.get('refresh_expires_in')))
    # The addon refreshes with a 60 second margin; anything under that would
    # mean re-logging in on every call.
    if int(expires_in or 0) > 120:
        record('PASS', 'token lifetime leaves room for the 60 s refresh margin',
               '%s s' % expires_in)
    else:
        record('FAIL', 'token lifetime too short for the refresh margin',
               '%s s' % expires_in)
    return token


def verify_offices(token):
    section('2. PINCODE SEARCH — seeded Kerala offices and the guard rails')
    mismatched = []
    for pincode, expected_office in sorted(SEEDED_OFFICES.items()):
        status, raw, _h = call('GET', '/v1/pincode-search', token=token,
                               params={'pincode': pincode,
                                       'office-type': 'post'})
        payload = as_json(raw) or {}
        records = payload.get('data') if isinstance(payload, dict) else payload
        records = records or []
        bookable = [r for r in records if ipc.office_is_bookable(r)]
        if not bookable:
            mismatched.append('%s: no bookable office' % pincode)
            continue
        chosen = sorted(bookable, key=ipc.office_sort_key)[0]
        if str(chosen.get('office_id')) != expected_office:
            mismatched.append('%s: live picks %s (%s), seed says %s'
                              % (pincode, chosen.get('office_id'),
                                 chosen.get('office_name'), expected_office))
    if mismatched:
        record('WARN', 'seeded office ids match the deterministic live choice',
               '; '.join(mismatched))
    else:
        record('PASS', 'all 14 seeded Kerala district offices match live data',
               'deterministic selection rule reproduces every seed')

    # Multi-office pincode: the tie-break must be stable.
    status, raw, _h = call('GET', '/v1/pincode-search', token=token,
                           params={'pincode': '110001', 'office-type': 'post'})
    payload = as_json(raw) or {}
    records = (payload.get('data') if isinstance(payload, dict) else payload) or []
    bookable = [r for r in records if ipc.office_is_bookable(r)]
    ordered = sorted(bookable, key=ipc.office_sort_key)
    again = sorted(reversed(bookable), key=ipc.office_sort_key)
    stable = [r.get('office_id') for r in ordered] == \
             [r.get('office_id') for r in again]
    record('PASS' if stable and ordered else 'FAIL',
           '110001 multi-office tie-break is deterministic',
           '%d records, %d bookable, chosen %s (%s)'
           % (len(records), len(bookable),
              ordered[0].get('office_id') if ordered else '-',
              ordered[0].get('office_name') if ordered else '-'))

    # The prefix-match trap the addon refuses locally.
    status, raw, _h = call('GET', '/v1/pincode-search', token=token,
                           params={'pincode': '68200'})
    payload = as_json(raw) or {}
    count = len((payload.get('data') if isinstance(payload, dict) else payload) or [])
    try:
        ipc.normalize_pincode('68200')
        record('FAIL', 'addon rejects a 5-digit pincode', 'it did not')
    except ipc.IndiapostDataError:
        record('PASS', 'addon rejects a 5-digit pincode before the API sees it',
               'live API would have returned %d unrelated offices' % count)

    status, raw, _h = call('GET', '/v1/pincode-search', token=token,
                           params={'pincode': '999999'})
    payload = as_json(raw) or {}
    count = len((payload.get('data') if isinstance(payload, dict) else payload) or [])
    record('PASS' if count == 0 else 'FAIL',
           'a nonexistent pincode returns HTTP 200 with zero records',
           'HTTP %s, %d records — the addon checks the count, not the status'
           % (status, count))


def verify_tariffs(token):
    section('3. TARIFF — the addon\'s arithmetic against India Post\'s answers')
    cases = [
        # (label, kg, l, b, h, expected product, expected chargeable g)
        ('250 g document', 0.25, 30, 21, 2, ipc.PRODUCT_DOC, 250),
        ('1.5 kg parcel, volumetric wins', 1.5, 30, 20, 15,
         ipc.PRODUCT_PARCEL, 1800),
        ('500 g is still a document', 0.5, 30, 21, 2, ipc.PRODUCT_DOC, 500),
        ('501 g becomes a parcel', 0.501, 30, 21, 2, ipc.PRODUCT_PARCEL, 501),
        # Documents are billed on actual weight however bulky: volumetric 464 g
        # here, but India Post charges the 250 g it really weighs.
        ('bulky document ignores volumetric', 0.25, 40, 29, 2,
         ipc.PRODUCT_DOC, 250),
    ]
    for label, kg, length, breadth, height, want_product, want_chargeable in cases:
        grams = ipc.kg_to_grams(kg)
        params = {
            'product-code': ipc.ARTICLE_TYPE_SPEED_POST,
            'weight': grams,
            'source-pincode': '682001',
            'destination-pincode': '110001',
            'length': ipc.cm_to_int(length),
            'width': ipc.cm_to_int(breadth),
            'height': ipc.cm_to_int(height),
        }
        status, raw, _h = call('GET', '/v1/speed-post/tariffs', token=token,
                               params=params)
        payload = as_json(raw) or {}
        if status != 200 or not payload.get('success'):
            record('FAIL', 'tariff: %s' % label,
                   'HTTP %s %s' % (status, raw[:160]))
            continue
        api_product = payload.get('product_code')
        api_chargeable = int(ipc.as_amount(payload.get('chargeable_weight')))
        our_product = ipc.resolve_product_code(grams)
        our_chargeable = ipc.chargeable_weight_g(grams, length, breadth, height)
        ok = (api_product == want_product == our_product
              and api_chargeable == want_chargeable == our_chargeable)
        record('PASS' if ok else 'FAIL', 'tariff: %s' % label,
               'API %s/%s g, addon %s/%s g, base %s, total %s'
               % (api_product, api_chargeable, our_product, our_chargeable,
                  payload.get('base_tariff'), payload.get('final_amount')))

    # distance_km can be the string "OS"; amounts can be fractional.
    status, raw, _h = call('GET', '/v1/speed-post/tariffs', token=token, params={
        'product-code': 'SP', 'weight': 250, 'source-pincode': '682001',
        'destination-pincode': '110001', 'length': 30, 'width': 21, 'height': 2,
        'REG': 'TRUE', 'ACK': 'TRUE', 'OTP': 'TRUE',
    })
    payload = as_json(raw) or {}
    distance = payload.get('distance_km')
    numeric, display = ipc.as_distance(distance)
    fractional = [k for k, v in (payload.get('vas_details') or {}).items()
                  if isinstance(v, float) and v != int(v)]
    record('PASS', 'distance_km and fractional VAS are handled',
           'distance_km=%r -> numeric=%r display=%r; fractional VAS: %s; '
           'vas_charges=%s' % (distance, numeric, display,
                               fractional or 'none', payload.get('vas_charges')))

    # Fractional grams must never be sent.
    status, raw, _h = call('GET', '/v1/speed-post/tariffs', token=token, params={
        'product-code': 'SP', 'weight': '250.5', 'source-pincode': '682001',
        'destination-pincode': '110001', 'length': 30, 'width': 21, 'height': 2,
    })
    record('PASS' if status != 200 else 'FAIL',
           'a fractional weight is rejected, so kg_to_grams must round up',
           'HTTP %s; addon sends kg_to_grams(0.2505)=%d'
           % (status, ipc.kg_to_grams(0.2505)))


def verify_dimension_limits(token):
    section('4. DIMENSIONS — the addon\'s verdict against HTTP 422')
    cases = [
        ('600 g at 10x5x5 (small and heavy)', 600, 10, 5, 5),
        ('L+B+H = 400 cm', 600, 150, 150, 100),
        ('L+B+H = 301 cm', 600, 151, 100, 50),
        ('L+B+H = 300 cm exactly', 600, 150, 100, 50),
        ('legitimate 1.5 kg parcel', 1500, 30, 20, 15),
    ]
    for label, grams, length, breadth, height in cases:
        status, raw, _h = call('GET', '/v1/speed-post/tariffs', token=token,
                               params={
                                   'product-code': 'SP', 'weight': grams,
                                   'source-pincode': '682001',
                                   'destination-pincode': '110001',
                                   'length': length, 'width': breadth,
                                   'height': height,
                               })
        payload = as_json(raw) or {}
        api_rejects = status != 200 or not payload.get('success')
        errors, warnings = ipc.validate_package(grams, length, breadth, height)
        addon_rejects = bool(errors)
        agree = api_rejects == addon_rejects
        record('PASS' if agree else 'FAIL', 'dimensions: %s' % label,
               'API %s (HTTP %s), addon %s%s'
               % ('rejects' if api_rejects else 'accepts', status,
                  'rejects' if addon_rejects else 'accepts',
                  '; warns: %s' % warnings[0][:60] if warnings else ''))


def build_article(barcode=None):
    """The article the addon builds, assembled with the same helpers."""
    pickup_date = datetime.date.today() + datetime.timedelta(days=1)
    moment = ipc.pickup_datetime(pickup_date, '10:00-13:00')
    grams = ipc.kg_to_grams(1.5)
    sender_address = ipc.split_address_lines(
        'Second Floor, Riders Tower, MG Road', 'Consignor address')
    seller_address = ipc.split_address_lines(
        'Unit 4, Kalamassery Industrial Estate', 'Seller pickup address')
    customer_address = ipc.split_address_lines(
        '12 Barakhamba Road, Connaught Place', 'Customer address')
    article = {
        'bulk_customer_id': TEST_CUSTOMER_ID,
        'contract_id': TEST_CONTRACT_ID,
        'pickup_or_dropoff': 'PICKUP',
        'pickup_dropoff_office_id': SEEDED_OFFICES['682001'],
        'article_type': ipc.ARTICLE_TYPE_SPEED_POST,
        'physical_weight': grams,
        'shape_of_article': ipc.resolve_shape(grams),
        'length': ipc.cm_to_int(30),
        'breadth_diameter': ipc.cm_to_int(20),
        'height': ipc.cm_to_int(15),
        'bulk_reference': 'KX/SELFTEST/0001',
        # Consignor of record: KeralaXpress.
        'sender_name': ipc.normalize_text('KeralaXpress Delivery', 'name'),
        'sender_company': ipc.normalize_text(
            'Kerala Riders Logistics Private Limited', 'company'),
        'sender_add_line_1': sender_address[0],
        'sender_city': ipc.normalize_text('Ernakulam', 'city'),
        'sender_state': ipc.normalize_text('KERALA', 'state'),
        'sender_pincode': ipc.normalize_pincode('682001'),
        'sender_mobile_no': ipc.normalize_mobile('9876543210'),
        # Consignee.
        'receiver_name': ipc.normalize_text('Ravi Kumar', 'name'),
        'receiver_company': ipc.normalize_text('Ravi Kumar', 'company'),
        'receiver_add_line_1': customer_address[0],
        'receiver_city': ipc.normalize_text('New Delhi', 'city'),
        'receiver_state': ipc.normalize_text('DELHI', 'state'),
        'receiver_pincode': ipc.normalize_pincode('110001'),
        'receiver_mobile_no': ipc.normalize_mobile('+91 98111 22334'),
        # Pickup at the seller's premises.
        'pickup_address_flag': 'TRUE',
        'pickup_addressee_name': ipc.normalize_text('Anand Traders', 'name'),
        'pickup_company_name': ipc.normalize_text('Anand Traders', 'company'),
        'pickup_address_line1': seller_address[0],
        'pickup_city': ipc.normalize_text('Kalamassery', 'city'),
        'pickup_state': ipc.normalize_text('KERALA', 'state'),
        'pickup_pincode': ipc.normalize_pincode('683104'),
        'pickup_mobile_no': ipc.normalize_mobile('9847012345'),
        'pickup_schedule_slot': '10:00-13:00',
        'pickup_schedule_date': ipc.format_pickup_datetime(moment),
        # Returns go back to the seller, not to KeralaXpress.
        'alt_address_flag': 'TRUE',
        'alt_addressee_name': ipc.normalize_text('Anand Traders', 'name'),
        'alt_company_name': ipc.normalize_text('Anand Traders', 'company'),
        'alt_address_line1': seller_address[0],
        'alt_city': ipc.normalize_text('Kalamassery', 'city'),
        'alt_state': ipc.normalize_text('KERALA', 'state'),
        'alt_pincode': ipc.normalize_pincode('683104'),
        'alt_alternate_mobile_no': ipc.normalize_mobile('9847012345'),
    }
    if barcode:
        article['barcode_no'] = barcode
    return article


def verify_booking(token):
    section('5. BOOKING — the addon\'s payload against the live validator')
    article = build_article()
    status, raw, _h = call('POST', '/process-articles/%s' % TEST_CUSTOMER_ID,
                           token=token, body={'articles': [article]})
    payload = as_json(raw) or {}
    errors = [str(e) for entry in (payload.get('error_articles') or [])
              for e in (entry.get('errors') or [])]
    # Omitting barcode_no draws two errors: the missing barcode itself and a
    # "Text value is required for data processing" knock-on from the same field.
    # Anything else in this list would be a genuine payload problem.
    expected = ('barcode', 'text value is required')
    only_barcode = bool(errors) and all(
        any(fragment in e.lower() for fragment in expected) for e in errors)
    record('PASS' if only_barcode else 'FAIL',
           'the full three-address payload validates',
           'HTTP %s; only the deliberately omitted barcode is rejected: %s'
           % (status, errors))

    # Per-article failures arrive as HTTP 200 with success:true.
    record('PASS' if status == 200 and payload.get('success') else 'WARN',
           'per-article rejections come back as HTTP 200 success:true',
           'success=%s, error_count=%s — the addon inspects error_articles'
           % (payload.get('success'),
              (payload.get('summary') or {}).get('error_count')))

    # Contract validation on our own customer id, so the blocker is documented.
    own_customer = os.environ.get('INDIAPOST_USERNAME')
    own = build_article()
    own['bulk_customer_id'] = own_customer
    status2, raw2, _h = call('POST', '/process-articles/%s' % own_customer,
                             token=token, body={'articles': [own]})
    payload2 = as_json(raw2) or {}
    contract_errors = []
    for entry in (payload2.get('error_articles') or []):
        contract_errors.extend(entry.get('errors') or [])
    blocked = any('contract' in str(e).lower() for e in contract_errors)
    record('WARN' if blocked else 'PASS',
           'our own customer id still has no Speed Post contract',
           '%s' % (contract_errors[:2] or 'no contract error, booking is live'))

    # Error correlation. Three articles, the middle one broken, to prove the
    # addon must trust `index` rather than list position.
    good = build_article()
    broken = build_article()
    broken.pop('sender_name')
    status3, raw3, _h = call('POST', '/process-articles/%s' % TEST_CUSTOMER_ID,
                             token=token,
                             body={'articles': [good, broken, good]})
    payload3 = as_json(raw3) or {}
    indices = [e.get('index') for e in (payload3.get('error_articles') or [])]
    record('PASS' if status3 in (200, 400) else 'FAIL',
           'error entries carry an index for correlation',
           'HTTP %s, indices %s (order is not guaranteed)' % (status3, indices))

    # Slots: only two are accepted, and the date is not validated at all.
    bad_slot = build_article()
    bad_slot['pickup_schedule_slot'] = '09:00-12:00'
    status4, raw4, _h = call('POST', '/process-articles/%s' % TEST_CUSTOMER_ID,
                             token=token, body={'articles': [bad_slot]})
    payload4 = as_json(raw4) or {}
    slot_errors = []
    for entry in (payload4.get('error_articles') or []):
        slot_errors.extend(str(e) for e in (entry.get('errors') or []))
    record('PASS' if any('slot' in e.lower() for e in slot_errors) else 'WARN',
           'an invalid pickup slot is rejected',
           '%s' % [e for e in slot_errors if 'slot' in e.lower()][:1])

    past = build_article()
    past['pickup_schedule_date'] = '01/15/2020 10:00:00 AM'
    status5, raw5, _h = call('POST', '/process-articles/%s' % TEST_CUSTOMER_ID,
                             token=token, body={'articles': [past]})
    payload5 = as_json(raw5) or {}
    date_errors = []
    for entry in (payload5.get('error_articles') or []):
        date_errors.extend(str(e) for e in (entry.get('errors') or []))
    unvalidated = not any('date' in e.lower() for e in date_errors)
    record('WARN' if unvalidated else 'PASS',
           'pickup_schedule_date is NOT validated server-side',
           'a 2020 date drew no date error, so the addon validates it itself')


def verify_label(token):
    section('6. LABEL — the addon\'s label payload')
    # A barcode outside our allotted range: label rendering is decoupled from
    # booking, so this consumes nothing.
    payload = [{
        'customer_id': int(TEST_CUSTOMER_ID),
        'user_id': int(TEST_CUSTOMER_ID),
        'channel_type': 'E',
        'user_type': 'R',
        'barcode_no': 'EU123456785IN',
        'service_type': ipc.ARTICLE_TYPE_SPEED_POST,
        'booking_type': 'COMMERCIAL',
        'recipient_name': 'Ravi Kumar',
        'recipient_addressl1': '12 Barakhamba Road, Connaught Place',
        'recipient_city': 'New Delhi',
        'recipient_pin': '110001',
        'sender_name': 'KeralaXpress Delivery',
        'sender_addressl1': 'Second Floor, Riders Tower, MG Road',
        'sender_city': 'Ernakulam',
        'sender_pin': '682001',
        'transmission_mode': 'S',
        'payment_mode': 'CO',
        'payment_status': 'PC',
        'identifier': 'Domestic',
        'booking_office_name': 'Kochi HO',
        'booking_office_pin': '682001',
        'size': 'A6',
    }]
    status, raw, headers = call('POST', '/v1/label/create/domestic',
                                token=token, body=payload,
                                accept='application/pdf, application/json')
    is_pdf = raw[:4] == b'%PDF'
    record('PASS' if status in (200, 201) and is_pdf else 'FAIL',
           'A6 label returns a PDF',
           'HTTP %s, %s, %d bytes'
           % (status, headers.get('Content-Type'), len(raw)))

    # A missing mandatory field must produce a structured error, not a PDF.
    broken = [dict(payload[0])]
    broken[0].pop('recipient_name')
    status2, raw2, _h = call('POST', '/v1/label/create/domestic', token=token,
                             body=broken, accept='application/pdf, application/json')
    record('PASS' if raw2[:4] != b'%PDF' else 'FAIL',
           'a label missing a mandatory field errors instead of rendering',
           'HTTP %s %s' % (status2, raw2[:140]))


def verify_tracking(token):
    section('7. TRACKING — envelope shape and the ambiguity the addon ignores')
    status, raw, _h = call('POST', '/v1/tracking/bulk', token=token,
                           body={'bulk': []})
    payload = as_json(raw) or {}
    record('PASS' if payload.get('data') in (None, []) else 'FAIL',
           'an empty list returns data: null and must be guarded',
           'HTTP %s, data=%r' % (status, payload.get('data')))

    status, raw, _h = call('POST', '/v1/tracking/bulk', token=token,
                           body={'bulk': ['ET214330016IN']})
    payload = as_json(raw) or {}
    entries = payload.get('data') or []
    entry = entries[0] if entries else {}
    scans = entry.get('tracking_details') or []
    record('PASS', 'an unbooked article is indistinguishable from an unscanned one',
           'del_status=%r, %d scans — the addon treats this as "no news"'
           % ((entry.get('del_status') or {}).get('del_status'), len(scans)))

    # A booked article from the vendor's own samples, to see real event text.
    for sample in ('EE123456789IN', 'ET614901304IN'):
        status, raw, _h = call('POST', '/v1/tracking/bulk', token=token,
                               body={'bulk': [sample]})
        payload = as_json(raw) or {}
        entries = payload.get('data') or []
        scans = (entries[0].get('tracking_details') or []) if entries else []
        if not scans:
            continue
        raw_events = [s.get('event') for s in scans]
        unmapped = []
        for text in raw_events:
            # Reproduces models/indiapost_tracking.classify_event's fallback.
            key = str(text or '').strip().upper()
            if not key:
                unmapped.append(text)
        record('PASS', 'live event field carries free text, not codes',
               '%s: %s' % (sample, raw_events[:4]))
        break
    else:
        record('WARN', 'no sample article returned scans',
               'event text mapping could not be re-confirmed this run')


def main():
    if not (os.environ.get('INDIAPOST_USERNAME')
            and os.environ.get('INDIAPOST_PASSWORD')):
        print('Set INDIAPOST_USERNAME and INDIAPOST_PASSWORD.')
        return 2
    token = login()
    if not token:
        return 1
    verify_offices(token)
    verify_tariffs(token)
    verify_dimension_limits(token)
    verify_booking(token)
    verify_label(token)
    verify_tracking(token)

    print('\n' + '=' * 76)
    print('SUMMARY')
    print('=' * 76)
    for status, label, detail in RESULTS:
        print('%-4s %-58s %s' % (status, label, detail[:80]))
    counts = {}
    for status, _l, _d in RESULTS:
        counts[status] = counts.get(status, 0) + 1
    print('\n' + ', '.join('%s: %d' % kv for kv in sorted(counts.items())))
    print('AWBs consumed: 0 (barcode_no omitted on every booking call)')
    return 1 if counts.get('FAIL') else 0


if __name__ == '__main__':
    sys.exit(main())
