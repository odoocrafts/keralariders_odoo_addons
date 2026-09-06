#!/usr/bin/env python3
"""Probe India Post label generation, bulk tracking and tariff VAS charges.

Also exercises the modulo-11 barcode generator against the allotted AWB range
so the digits can be checked before any real booking is attempted.

Nothing here books an article, so no AWB is consumed. Label generation is a
rendering call and tracking is read-only.

    export INDIAPOST_USERNAME=... INDIAPOST_PASSWORD=...
    python3 indiapost_probe_extras.py
"""

import json
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

# Allotted UAT range is ET21433001XIN to ET21434000XIN, where X is the
# position-11 check digit we compute.
AWB_PREFIX = "ET"
AWB_FIRST_SERIAL = 21433001
AWB_LAST_SERIAL = 21434000

# Weighting factors from the barcode generation logic document.
WEIGHTS = (8, 6, 4, 2, 3, 5, 9, 7)


def check_digit(serial):
    """Weighted modulo 11 check digit over the 8-digit serial."""
    digits = "%08d" % int(serial)
    total = sum(int(d) * w for d, w in zip(digits, WEIGHTS))
    remainder = total % 11
    if remainder == 0:
        return "5"
    if remainder == 1:
        return "0"
    return str(11 - remainder)


def barcode(serial):
    return "%s%08d%s%s" % (AWB_PREFIX, int(serial), check_digit(serial), "IN")


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


def brief(raw, limit=900):
    try:
        return json.dumps(json.loads(raw.decode("utf-8", "replace")),
                          indent=2)[:limit]
    except (ValueError, TypeError):
        return raw.decode("utf-8", "replace")[:limit]


def login():
    status, raw, _ = call(
        "POST", "/v1/access/login",
        body={"username": os.environ.get("INDIAPOST_USERNAME"),
              "password": os.environ.get("INDIAPOST_PASSWORD")},
    )
    if status != 200:
        print("login failed HTTP %s: %s" % (status, brief(raw, 300)))
        return None
    return (json.loads(raw).get("data") or {}).get("access_token")


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def check_barcodes():
    section("BARCODE GENERATOR (weighted modulo 11)")
    # The worked example from the barcode logic document: serial 47312482
    # should yield check digit 9.
    expected = "9"
    got = check_digit(47312482)
    print("document worked example 47312482 -> %s (expected %s) %s"
          % (got, expected, "OK" if got == expected else "MISMATCH"))
    print("\nfirst and last of our allotted range:")
    print("  serial %d -> %s" % (AWB_FIRST_SERIAL, barcode(AWB_FIRST_SERIAL)))
    print("  serial %d -> %s" % (AWB_LAST_SERIAL, barcode(AWB_LAST_SERIAL)))
    print("\ncheck digit distribution across all %d allotted serials:"
          % (AWB_LAST_SERIAL - AWB_FIRST_SERIAL + 1))
    counts = {}
    for s in range(AWB_FIRST_SERIAL, AWB_LAST_SERIAL + 1):
        d = check_digit(s)
        counts[d] = counts.get(d, 0) + 1
    print("  " + "  ".join("%s:%d" % (k, counts[k])
                           for k in sorted(counts)))
    if len({barcode(s) for s in range(AWB_FIRST_SERIAL, AWB_LAST_SERIAL + 1)}) \
            == AWB_LAST_SERIAL - AWB_FIRST_SERIAL + 1:
        print("  all generated barcodes are unique")


def probe_tariff_vas(token):
    section("SPEED POST TARIFF, VALUE ADDED SERVICES")
    base = {"product-code": "SP", "weight": 250,
            "source-pincode": "682001", "destination-pincode": "110001",
            "length": 30, "width": 21, "height": 2}
    variants = [
        ("baseline, no VAS", {}),
        ("INS=1000", {"INS": 1000}),
        ("POD=YES", {"POD": "YES"}),
        ("INS=1000 + POD=YES", {"INS": 1000, "POD": "YES"}),
        ("INS=50000 (high declared value)", {"INS": 50000}),
        ("REG/ACK/OTP flags (documented for NDD only)",
         {"REG": "TRUE", "ACK": "TRUE", "OTP": "TRUE"}),
    ]
    for label, extra in variants:
        params = dict(base)
        params.update(extra)
        status, raw, _ = call("GET", "/v1/speed-post/tariffs",
                              token=token, params=params)
        try:
            p = json.loads(raw)
        except ValueError:
            print("%-44s HTTP %s  %s" % (label, status, brief(raw, 120)))
            continue
        if not p.get("success"):
            print("%-44s HTTP %s  %s"
                  % (label, status, p.get("error") or p.get("message")))
            continue
        print("%-44s base=%-5s vas=%-5s tax=%-4s total=%-5s  %s"
              % (label, p.get("base_tariff"), p.get("vas_charges"),
                 p.get("total_tax"), p.get("final_amount"),
                 json.dumps(p.get("vas_details") or {})))


def probe_label(token):
    section("ADDRESS LABEL GENERATION")
    awb = barcode(AWB_FIRST_SERIAL)
    payload = [{
        "customer_id": int(CUSTOMER_ID),
        "delivery_office_name": "New Delhi GPO",
        "destination_pin": "110001",
        "booking_datetime": "06-09-2026 13:28:20",
        "channel_type": "E",
        "user_type": "R",
        "user_id": int(CUSTOMER_ID),
        "barcode_no": awb,
        "service_type": "SP",
        "booking_type": "COMMERCIAL",
        "article_length": "30",
        "article_breadth": "21",
        "article_height": "2",
        "charged_weight": 250,
        "physical_weight": 250,
        "volumetric_weight": 252,
        "insurance_flag": False,
        "insurance_value": 0,
        "recipient_name": "Test Receiver",
        "recipient_mobile": "9876543210",
        "recipient_addressl1": "New Delhi GPO",
        "recipient_city": "CENTRAL DELHI",
        "recipient_pin": "110001",
        "recipient_state": "DELHI",
        "sender_name": "KeralaXpress Delivery",
        "sender_mobile": "9400662693",
        "sender_addressl1": "Kochi Head Office",
        "sender_city": "ERNAKULAM",
        "sender_pin": "682001",
        "sender_state": "KERALA",
        "transmission_mode": "S",
        "payment_mode": "CO",
        "booking_office_name": "Kochi HO",
        "booking_office_pin": "682001",
        "size": "A6",
        "total_amount": 91,
        "payment_status": "PC",
        "identifier": "Domestic",
        "priority": False,
        "registered_flag": False,
    }]

    for size in ("A6", "A7"):
        payload[0]["size"] = size
        status, raw, headers = call("POST", "/v1/label/create/domestic",
                                    token=token, body=payload)
        ctype = headers.get("Content-Type", "?")
        print("\nsize=%s -> HTTP %s  content-type=%s  bytes=%d"
              % (size, status, ctype, len(raw)))
        if raw[:4] == b"%PDF":
            path = "/tmp/indiapost_label_%s.pdf" % size
            with open(path, "wb") as fh:
                fh.write(raw)
            print("  PDF received for %s, saved to %s" % (awb, path))
        else:
            print("  " + brief(raw, 600))


def probe_tracking(token):
    section("BULK TRACKING")
    # Barcodes from the approach document, booked under other customers.
    # Expected to fail the ownership rule, which tells us how that is enforced.
    others = ["RK775227016IN", "EY011867595IN", "EB126023474IN"]
    status, raw, _ = call("POST", "/v1/tracking/bulk", token=token,
                          body={"bulk": others})
    print("articles booked by others -> HTTP %s" % status)
    print(brief(raw, 1100))

    ours = [barcode(AWB_FIRST_SERIAL)]
    status, raw, _ = call("POST", "/v1/tracking/bulk", token=token,
                          body={"bulk": ours})
    print("\nour unbooked AWB %s -> HTTP %s" % (ours[0], status))
    print(brief(raw, 700))

    status, raw, _ = call("POST", "/v1/tracking/bulk", token=token,
                          body={"bulk": []})
    print("\nempty list -> HTTP %s" % status)
    print(brief(raw, 400))


def probe_pincode_paging(token):
    section("PINCODE SEARCH PAGING AND EDGE CASES")
    for label, params in [
        ("Kochi 682001", {"pincode": "682001", "office-type": "post"}),
        ("Delhi 110001", {"pincode": "110001", "office-type": "post"}),
        ("limit=2 skip=0", {"pincode": "110001", "office-type": "post",
                            "limit": 2, "skip": 0}),
        ("no office-type", {"pincode": "682001"}),
        ("5-digit pincode", {"pincode": "68200", "office-type": "post"}),
        ("nonexistent 999999", {"pincode": "999999", "office-type": "post"}),
    ]:
        status, raw, _ = call("GET", "/v1/pincode-search",
                              token=token, params=params)
        try:
            p = json.loads(raw)
            count = p.get("returned_records_count") if isinstance(p, dict) else len(p)
            msg = p.get("message") or p.get("error") if isinstance(p, dict) else ""
            print("%-22s HTTP %s  records=%s  limit=%s  %s"
                  % (label, status, count,
                     p.get("limit") if isinstance(p, dict) else "-", msg))
        except ValueError:
            print("%-22s HTTP %s  %s" % (label, status, brief(raw, 150)))


def main():
    check_barcodes()
    if not os.environ.get("INDIAPOST_USERNAME"):
        print("\nset INDIAPOST_USERNAME and INDIAPOST_PASSWORD to probe the API")
        return 2
    token = login()
    if not token:
        return 2
    probe_tariff_vas(token)
    probe_pincode_paging(token)
    probe_label(token)
    probe_tracking(token)
    print("\nNo booking calls were made; no AWBs consumed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
