#!/usr/bin/env python3
"""Verify the exact request shapes the Odoo India Post integration will emit.

This mirrors the formats produced by keralariders_logistics/models/indiapost_*.py
so the addon can be trusted before production access is granted. Every booking
call deliberately omits ``barcode_no`` so each article is rejected and no
barcode from the allotted UAT range is consumed.

    export INDIAPOST_USERNAME=9999537187 INDIAPOST_PASSWORD='Dop@1234'
    python3 indiapost_integration_probe.py
"""

import datetime
import json
import math
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://test.cept.gov.in/beextcustomer"
TIMEOUT = 60
CUSTOMER_ID = os.environ.get("INDIAPOST_USERNAME", "9999537187")
# Contract 41585456 belongs to the document's test customer 3000064781 and is the
# only Speed Post contract known to pass contract validation.
TEST_CUSTOMER_ID = "3000064781"
TEST_CONTRACT_ID = "41585456"

WEIGHTS = (8, 6, 4, 2, 3, 5, 9, 7)


def check_digit(serial):
    digits = "%08d" % int(serial)
    remainder = sum(int(d) * w for d, w in zip(digits, WEIGHTS)) % 11
    if remainder == 0:
        return "5"
    if remainder == 1:
        return "0"
    return str(11 - remainder)


def barcode(serial, prefix="ET"):
    return "%s%08d%s%s" % (prefix, int(serial), check_digit(serial), "IN")


def call(method, path, token=None, params=None, body=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    req.add_header("Accept", "*/*")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)
    except (urllib.error.URLError, ssl.SSLError, socket.error) as exc:
        return 0, ("TRANSPORT ERROR: %r" % (exc,)).encode(), {}


def as_json(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None


def brief(raw, limit=900):
    payload = as_json(raw)
    if payload is None:
        return raw.decode("utf-8", "replace")[:limit]
    return json.dumps(payload, indent=2)[:limit]


def section(title):
    print("\n" + "=" * 76)
    print(title)
    print("=" * 76)


def login():
    status, raw, _h = call(
        "POST", "/v1/access/login",
        body={"username": os.environ.get("INDIAPOST_USERNAME"),
              "password": os.environ.get("INDIAPOST_PASSWORD")},
    )
    payload = as_json(raw) or {}
    data = payload.get("data") or {}
    section("1. LOGIN / TOKEN MANAGER ASSUMPTIONS")
    print("HTTP %s success=%s" % (status, payload.get("success")))
    print("expires_in=%s refresh_expires_in=%s has_refresh_token=%s"
          % (data.get("expires_in"), data.get("refresh_expires_in"),
             bool(data.get("refresh_token"))))
    return data.get("access_token")


# ---------------------------------------------------------------------------
# Mirrors IndiapostOffice._select_office deterministic ranking
# ---------------------------------------------------------------------------
OFFICE_TYPE_RANK = {"HPO": 0, "GPO": 0, "SPO": 1, "MDG": 2, "IDC": 3}


def rank_office(rec):
    return (
        OFFICE_TYPE_RANK.get((rec.get("office_type_code") or "").upper(), 9),
        str(rec.get("office_name") or ""),
        str(rec.get("office_id") or ""),
    )


def probe_offices(token):
    section("2. PINCODE / OFFICE RESOLUTION (deterministic selection)")
    for pincode in ("682001", "110001", "695001", "680001"):
        status, raw, _h = call("GET", "/v1/pincode-search", token=token,
                               params={"pincode": pincode,
                                       "office-type": "post"})
        payload = as_json(raw)
        records = payload.get("data") if isinstance(payload, dict) else payload
        records = [r for r in (records or []) if isinstance(r, dict)]
        bookable = [r for r in records
                    if r.get("delivery_office_flag")
                    and (r.get("office_type_code") or "").upper() != "BPO"]
        bookable.sort(key=rank_office)
        chosen = bookable[0] if bookable else None
        print("%s HTTP %s returned=%s bookable=%s -> chosen %s %s (%s)"
              % (pincode, status,
                 payload.get("returned_records_count")
                 if isinstance(payload, dict) else len(records),
                 len(bookable),
                 chosen and chosen.get("office_id"),
                 chosen and chosen.get("office_name"),
                 chosen and chosen.get("office_type_code")))
        if pincode == "110001" and len(bookable) > 1:
            print("   top 3 after ranking: %s"
                  % [(o.get("office_id"), o.get("office_type_code"),
                      o.get("office_name")) for o in bookable[:3]])
            # Stability: re-request and confirm the same winner.
            status2, raw2, _h2 = call("GET", "/v1/pincode-search", token=token,
                                      params={"pincode": pincode,
                                              "office-type": "post"})
            p2 = as_json(raw2) or {}
            recs2 = [r for r in (p2.get("data") or []) if isinstance(r, dict)]
            b2 = sorted([r for r in recs2
                         if r.get("delivery_office_flag")
                         and (r.get("office_type_code") or "").upper() != "BPO"],
                        key=rank_office)
            print("   repeat call chooses %s (stable=%s)"
                  % (b2 and b2[0].get("office_id"),
                     bool(b2) and b2[0].get("office_id") == chosen.get("office_id")))


# ---------------------------------------------------------------------------
# Mirrors IndiapostTariff dimension validation + Kg -> grams conversion
# ---------------------------------------------------------------------------
def kg_to_grams(kg):
    grams = int(math.ceil(float(kg) * 1000.0 - 1e-9))
    return max(grams, 1)


def probe_tariff(token):
    section("3. TARIFF: weight conversion, volumetric, limits, VAS")
    print("Kg -> grams rounding: 0.25->%s 1.5->%s 0.0004->%s 2.0001->%s"
          % (kg_to_grams(0.25), kg_to_grams(1.5),
             kg_to_grams(0.0004), kg_to_grams(2.0001)))
    cases = [
        ("doc 250g 30x21x2", {"weight": 250, "length": 30, "width": 21, "height": 2}),
        ("parcel 1500g 30x20x15 (vol divisor 5 -> 1800)",
         {"weight": 1500, "length": 30, "width": 20, "height": 15}),
        ("small heavy 600g 10x5x5 (expect 422)",
         {"weight": 600, "length": 10, "width": 5, "height": 5}),
        ("total dims 400cm 600g 150x150x100 (expect 422)",
         {"weight": 600, "length": 150, "width": 150, "height": 100}),
        ("doc over height 250g 30x21x5 (expect 422)",
         {"weight": 250, "length": 30, "width": 21, "height": 5}),
        ("parcel at floor 600g 14x9x1", {"weight": 600, "length": 14, "width": 9, "height": 1}),
        ("35kg max 35000g 50x40x30",
         {"weight": 35000, "length": 50, "width": 40, "height": 30}),
    ]
    for label, extra in cases:
        params = {"product-code": "SP", "source-pincode": "682001",
                  "destination-pincode": "110001"}
        params.update(extra)
        status, raw, _h = call("GET", "/v1/speed-post/tariffs", token=token,
                               params=params)
        p = as_json(raw) or {}
        if not p.get("success"):
            print("%-46s HTTP %s  %s"
                  % (label, status, (p.get("error") or p.get("message") or "")[:120]))
            continue
        print("%-46s HTTP %s product=%-17s chg_wt=%-6s dist=%-6r base=%-7s tax=%-6s total=%-7s"
              % (label, status, p.get("product_code"), p.get("chargeable_weight"),
                 p.get("distance_km"), p.get("base_tariff"),
                 p.get("total_tax"), p.get("final_amount")))

    print("\nVAS combinations (250g 30x21x2, 682001 -> 110001):")
    for label, extra in [
        ("none", {}),
        ("POD", {"POD": "YES"}),
        ("REG+ACK+OTP", {"REG": "TRUE", "ACK": "TRUE", "OTP": "TRUE"}),
        ("INS 1000", {"INS": 1000}),
        ("INS 50000", {"INS": 50000}),
        ("all", {"POD": "YES", "REG": "TRUE", "ACK": "TRUE", "OTP": "TRUE",
                 "INS": 5000}),
    ]:
        params = {"product-code": "SP", "weight": 250, "source-pincode": "682001",
                  "destination-pincode": "110001", "length": 30, "width": 21,
                  "height": 2}
        params.update(extra)
        status, raw, _h = call("GET", "/v1/speed-post/tariffs", token=token,
                               params=params)
        p = as_json(raw) or {}
        print("  %-14s base=%-7s vas=%-8s tax=%-7s total=%-8s %s"
              % (label, p.get("base_tariff"), p.get("vas_charges"),
                 p.get("total_tax"), p.get("final_amount"),
                 json.dumps(p.get("vas_details") or {})))

    print("\nLocal (same pincode) 682001 -> 682001:")
    status, raw, _h = call("GET", "/v1/speed-post/tariffs", token=token,
                           params={"product-code": "SP", "weight": 250,
                                   "source-pincode": "682001",
                                   "destination-pincode": "682001",
                                   "length": 30, "width": 21, "height": 2})
    p = as_json(raw) or {}
    print("  is_local=%r distance=%r base=%s total=%s"
          % (p.get("is_local"), p.get("distance_km"), p.get("base_tariff"),
             p.get("final_amount")))


# ---------------------------------------------------------------------------
# Mirrors IndiapostBooking._prepare_article_payload
# ---------------------------------------------------------------------------
def pickup_datetime(days_ahead=2):
    dt = datetime.datetime.now() + datetime.timedelta(days=days_ahead)
    dt = dt.replace(hour=10, minute=0, second=0, microsecond=0)
    return dt.strftime("%m/%d/%Y %I:%M:%S %p")


def article_payload(customer_id, contract_id, with_barcode=None, **overrides):
    payload = {
        "bulk_customer_id": customer_id,
        "contract_id": contract_id,
        "pickup_or_dropoff": "PICKUP",
        "pickup_dropoff_office_id": "22360020",
        "article_type": "SP",
        "physical_weight": 1500,
        "shape_of_article": "NROL",
        "length": 30,
        "breadth_diameter": 20,
        "height": 15,
        "bulk_reference": "2609/0042",
        # Consignor of record is KeralaXpress.
        "sender_name": "KeralaXpress Delivery",
        "sender_company": "KeralaXpress Logistics Private Limited",
        "sender_add_line_1": "Door 12/345, MG Road, Kochi",
        "sender_city": "Ernakulam",
        "sender_state": "Kerala",
        "sender_pincode": "682001",
        "sender_mobile_no": "9400662693",
        "sender_emailid": "keralaxpressdelivery@gmail.com",
        "receiver_name": "Test Receiver Name",
        "receiver_company": "Test Receiver Company",
        "receiver_add_line_1": "Connaught Place, Block A, New Delhi",
        "receiver_city": "New Delhi",
        "receiver_state": "Delhi",
        "receiver_pincode": "110001",
        "receiver_mobile_no": "9876543210",
        # Pickup happens at the seller's own premises.
        "pickup_address_flag": "TRUE",
        "pickup_addressee_name": "Kochi Seller Name",
        "pickup_company_name": "Kochi Seller Traders",
        "pickup_address_line1": "Shop 4, Broadway, Ernakulam",
        "pickup_city": "Ernakulam",
        "pickup_state": "Kerala",
        "pickup_pincode": "682001",
        "pickup_mobile_no": "9847012345",
        "pickup_schedule_slot": "10:00-13:00",
        "pickup_schedule_date": pickup_datetime(),
        # Undelivered articles must come back to the seller, not to us.
        "alt_address_flag": "TRUE",
        "alt_addressee_name": "Kochi Seller Name",
        "alt_company_name": "Kochi Seller Traders",
        "alt_address_line1": "Shop 4, Broadway, Ernakulam",
        "alt_city": "Ernakulam",
        "alt_state": "Kerala",
        "alt_pincode": "682001",
        "alt_alternate_mobile_no": "9847012345",
    }
    if with_barcode:
        payload["barcode_no"] = with_barcode
    payload.update(overrides)
    return payload


def show_booking(label, token, customer_id, articles):
    status, raw, _h = call("POST", "/process-articles/%s" % customer_id,
                           token=token, body={"articles": articles})
    p = as_json(raw)
    print("\n--- %s" % label)
    print("HTTP %s" % status)
    if not isinstance(p, dict):
        print(brief(raw, 400))
        return
    print("success=%s total=%s processed=%s summary=%s"
          % (p.get("success"), p.get("total"), p.get("processed"),
             json.dumps(p.get("summary") or {})))
    if p.get("error") or p.get("message"):
        print("error/message: %s" % (p.get("error") or p.get("message")))
    for art in (p.get("valid_articles") or []):
        print("  VALID   idx=%s barcode=%s tariff=%s"
              % (art.get("index"), art.get("barcode_no"),
                 art.get("calculated_tariff")))
    for art in (p.get("error_articles") or []):
        print("  ERROR   idx=%s barcode=%s errors=%s"
              % (art.get("index"), art.get("barcode_no"),
                 json.dumps(art.get("errors"))))


def probe_booking(token):
    section("4. BULK BOOKING PAYLOAD VALIDATION (barcode_no omitted, 0 AWBs used)")
    print("Endpoint has no /v1 prefix. pickup_schedule_date format: %s"
          % pickup_datetime())

    show_booking("our customer %s, our (missing) contract" % CUSTOMER_ID,
                 token, CUSTOMER_ID,
                 [article_payload(CUSTOMER_ID, "41585456")])

    show_booking("document test customer %s contract %s"
                 % (TEST_CUSTOMER_ID, TEST_CONTRACT_ID),
                 token, TEST_CUSTOMER_ID,
                 [article_payload(TEST_CUSTOMER_ID, TEST_CONTRACT_ID)])

    # Batch of three to confirm per-article index reporting.
    show_booking("batch of 3 (index mapping)", token, TEST_CUSTOMER_ID,
                 [article_payload(TEST_CUSTOMER_ID, TEST_CONTRACT_ID,
                                  bulk_reference="2609/000%d" % i)
                  for i in (1, 2, 3)])

    # Deliberately bad values: confirm the validator catches what our
    # ValidationError layer is supposed to pre-empt.
    show_booking("short city (2 chars) + 9-digit mobile + bad slot",
                 token, TEST_CUSTOMER_ID,
                 [article_payload(TEST_CUSTOMER_ID, TEST_CONTRACT_ID,
                                  receiver_city="NY",
                                  receiver_mobile_no="987654321",
                                  pickup_schedule_slot="09:00-12:00")])

    show_booking("mobile starting with 5", token, TEST_CUSTOMER_ID,
                 [article_payload(TEST_CUSTOMER_ID, TEST_CONTRACT_ID,
                                  receiver_mobile_no="5876543210")])

    show_booking("DOC shape under 500g", token, TEST_CUSTOMER_ID,
                 [article_payload(TEST_CUSTOMER_ID, TEST_CONTRACT_ID,
                                  physical_weight=250, shape_of_article="DOC",
                                  length=30, breadth_diameter=21, height=2)])

    show_booking("COD article", token, TEST_CUSTOMER_ID,
                 [article_payload(TEST_CUSTOMER_ID, TEST_CONTRACT_ID,
                                  codr_cod="COD", value_for_codr_cod=1500)])

    show_booking("alt_address_flag TRUE but alt fields blank",
                 token, TEST_CUSTOMER_ID,
                 [article_payload(TEST_CUSTOMER_ID, TEST_CONTRACT_ID,
                                  alt_addressee_name="", alt_company_name="",
                                  alt_address_line1="", alt_city="",
                                  alt_pincode="", alt_alternate_mobile_no="")])

    show_booking("no articles key (payload-level error)", token,
                 TEST_CUSTOMER_ID, [])


def probe_label(token):
    section("5. LABEL CREATE (decoupled from booking, no AWB consumed)")
    awb = barcode(21433001)
    payload = [{
        "customer_id": int(CUSTOMER_ID),
        "channel_type": "E",
        "user_type": "R",
        "user_id": int(CUSTOMER_ID),
        "barcode_no": awb,
        "service_type": "SP",
        "booking_type": "COMMERCIAL",
        "recipient_name": "Test Receiver Name",
        "recipient_addressl1": "Connaught Place, Block A, New Delhi",
        "recipient_city": "New Delhi",
        "recipient_pin": "110001",
        "recipient_state": "Delhi",
        "recipient_mobile": "9876543210",
        "sender_name": "KeralaXpress Delivery",
        "sender_addressl1": "Door 12/345, MG Road, Kochi",
        "sender_city": "Ernakulam",
        "sender_pin": "682001",
        "sender_state": "Kerala",
        "sender_mobile": "9400662693",
        "transmission_mode": "S",
        "payment_mode": "CO",
        "booking_office_name": "Ernakulam HO",
        "booking_office_pin": "682001",
        "size": "A6",
        "payment_status": "PC",
        "identifier": "Domestic",
        "physical_weight": 1500,
        "charged_weight": 1800,
        "article_length": "30",
        "article_breadth": "20",
        "article_height": "15",
    }]
    for size in ("A6", "A7"):
        payload[0]["size"] = size
        status, raw, headers = call("POST", "/v1/label/create/domestic",
                                    token=token, body=payload)
        print("size=%s HTTP %s content-type=%s bytes=%d pdf=%s"
              % (size, status, headers.get("Content-Type"), len(raw),
                 raw[:4] == b"%PDF"))
        if raw[:4] != b"%PDF":
            print("   " + brief(raw, 300))

    # Two labels in one array — the addon batches per shipment, confirm the
    # array form still returns a single PDF and not an error.
    two = [dict(payload[0]), dict(payload[0], barcode_no=barcode(21433002))]
    status, raw, headers = call("POST", "/v1/label/create/domestic",
                                token=token, body=two)
    print("two barcodes in one array -> HTTP %s bytes=%d pdf=%s"
          % (status, len(raw), raw[:4] == b"%PDF"))

    # Missing mandatory field: confirm error shape is JSON, not a broken PDF.
    bad = [dict(payload[0])]
    bad[0].pop("recipient_addressl1")
    status, raw, _h = call("POST", "/v1/label/create/domestic", token=token,
                           body=bad)
    print("missing recipient_addressl1 -> HTTP %s  %s" % (status, brief(raw, 300)))


def probe_tracking(token):
    section("6. BULK TRACKING")
    status, raw, _h = call("POST", "/v1/tracking/bulk", token=token,
                           body={"bulk": []})
    p = as_json(raw) or {}
    print("empty list -> HTTP %s success=%s data=%r"
          % (status, p.get("success"), p.get("data")))

    unbooked = barcode(21433001)
    status, raw, _h = call("POST", "/v1/tracking/bulk", token=token,
                           body={"bulk": [unbooked]})
    p = as_json(raw) or {}
    rec = ((p.get("data") or [{}]) or [{}])[0] if p.get("data") else {}
    print("unbooked %s -> HTTP %s events=%s del_status=%r"
          % (unbooked, status, len(rec.get("tracking_details") or []),
             (rec.get("del_status") or {}).get("del_status")
             if isinstance(rec.get("del_status"), dict) else rec.get("del_status")))

    others = ["RK775227016IN", "EY011867595IN", "EB126023474IN"]
    status, raw, _h = call("POST", "/v1/tracking/bulk", token=token,
                           body={"bulk": others})
    p = as_json(raw) or {}
    print("three foreign barcodes -> HTTP %s success=%s records=%s"
          % (status, p.get("success"), len(p.get("data") or [])))
    for rec in (p.get("data") or []):
        events = rec.get("tracking_details") or []
        codes = sorted({e.get("event") for e in events if e.get("event")})
        print("   events=%-3s del_status=%r event codes seen: %s"
              % (len(events),
                 (rec.get("del_status") or {}).get("del_status")
                 if isinstance(rec.get("del_status"), dict) else rec.get("del_status"),
                 codes))

    # Mixed known-good + unknown, which is what the cron will send.
    status, raw, _h = call("POST", "/v1/tracking/bulk", token=token,
                           body={"bulk": [others[0], unbooked, "XX000000000XX"]})
    p = as_json(raw) or {}
    print("mixed batch of 3 -> HTTP %s records=%s"
          % (status, len(p.get("data") or [])))


def probe_barcodes():
    section("7. BARCODE CHECK DIGIT")
    print("document example 47312482 -> %s (expect 9)" % check_digit(47312482))
    print("range first %s  last %s" % (barcode(21433001), barcode(21434000)))
    uniq = {barcode(s) for s in range(21433001, 21434001)}
    print("1000 serials -> %d unique barcodes" % len(uniq))


def main():
    probe_barcodes()
    if not os.environ.get("INDIAPOST_USERNAME"):
        print("\nset INDIAPOST_USERNAME / INDIAPOST_PASSWORD")
        return 2
    token = login()
    if not token:
        print("login failed")
        return 2
    probe_offices(token)
    probe_tariff(token)
    probe_booking(token)
    probe_label(token)
    probe_tracking(token)
    print("\nDone. barcode_no was omitted on every booking call; 0 AWBs consumed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
