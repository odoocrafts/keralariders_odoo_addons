#!/usr/bin/env python3
"""Check Kerala pickup viability against the India Post sandbox.

Two things are verified:

1. Office coverage. India Post collects from each seller's own address, so
   every Kerala pincode we serve must resolve to at least one bookable office
   (delivery_office_flag true, office_type_code not BPO). A pincode with no
   bookable office cannot be used for pickup.

2. Pickup-mode booking validation. The approach document has no standalone
   pickup API -- pickup lives inside the booking payload -- so this sends a
   PICKUP article to learn which pickup fields the validator actually enforces.

The booking article omits barcode_no, which is mandatory, so it is always
rejected and no AWB from our allotted range is consumed.

    export INDIAPOST_USERNAME=... INDIAPOST_PASSWORD=...
    python3 indiapost_pickup_probe.py
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
TIMEOUT = 45

# Our own sandbox customer id has no contract attached yet, so booking
# validation has to run against the customer/contract pair published in the
# approach document.
TEST_CUSTOMER = "3000064781"
SP_CONTRACT = "41585456"

# District headquarters pincode for each of the 14 hubs in hubs.xml.
KERALA_HQ = [
    ("Kasargod", "671121"),
    ("Kannur", "670001"),
    ("Wayanad", "673121"),
    ("Kozhikode", "673001"),
    ("Malappuram", "676505"),
    ("Palakkad", "678001"),
    ("Thrissur", "680001"),
    ("Ernakulam", "682001"),
    ("Idukki", "685603"),
    ("Kottayam", "686001"),
    ("Alappuzha", "688001"),
    ("Pathanamthitta", "689645"),
    ("Kollam", "691001"),
    ("Thiruvananthapuram", "695001"),
]


def call(method, path, token=None, params=None, body=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, ssl.SSLError, socket.error) as exc:
        return 0, "TRANSPORT ERROR: %r" % (exc,)


def records(raw):
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [r for r in payload["data"] if isinstance(r, dict)]
    return []


def login():
    username = os.environ.get("INDIAPOST_USERNAME")
    password = os.environ.get("INDIAPOST_PASSWORD")
    if not username or not password:
        return None
    status, raw = call("POST", "/v1/access/login",
                       body={"username": username, "password": password})
    if status != 200:
        print("login failed: HTTP %s %s" % (status, raw[:300]))
        return None
    return (json.loads(raw).get("data") or {}).get("access_token")


def check_coverage(token):
    print("=" * 72)
    print("KERALA OFFICE COVERAGE (pickup requires a bookable office)")
    print("=" * 72)
    gaps = []
    for district, pincode in KERALA_HQ:
        status, raw = call("GET", "/v1/pincode-search", token=token,
                           params={"pincode": pincode, "office-type": "post"})
        offices = records(raw)
        bookable = [o for o in offices
                    if o.get("delivery_office_flag")
                    and o.get("office_type_code") != "BPO"]
        if status != 200:
            print("%-20s %s  HTTP %s  %s"
                  % (district, pincode, status, raw[:90]))
            gaps.append((district, pincode, "HTTP %s" % status))
            continue
        if not bookable:
            kinds = ",".join(sorted({o.get("office_type_code") or "?"
                                     for o in offices})) or "none"
            print("%-20s %s  NO BOOKABLE OFFICE (%d returned, types: %s)"
                  % (district, pincode, len(offices), kinds))
            gaps.append((district, pincode, "no bookable office"))
            continue
        first = bookable[0]
        print("%-20s %s  %s  %-34s %s  (%d of %d bookable)"
              % (district, pincode, first.get("office_id"),
                 first.get("office_name"), first.get("office_type_code"),
                 len(bookable), len(offices)))
    print()
    if gaps:
        print("COVERAGE GAPS: %d of %d districts unusable for pickup"
              % (len(gaps), len(KERALA_HQ)))
        for district, pincode, reason in gaps:
            print("  %s (%s): %s" % (district, pincode, reason))
    else:
        print("All %d district HQ pincodes resolve to a bookable office."
              % len(KERALA_HQ))
    print()


def pickup_article():
    """A PICKUP article. barcode_no omitted so it can never book."""
    return {
        "bulk_customer_id": TEST_CUSTOMER,
        "contract_id": SP_CONTRACT,
        "pickup_or_dropoff": "PICKUP",
        "pickup_dropoff_office_id": 22360020,   # Kochi HO
        "article_type": "SP",
        "physical_weight": 250,
        "shape_of_article": "DOC",
        "length": 30,
        "breadth_diameter": 21,
        "height": 2,
        # Consignor of record is KeralaXpress.
        "sender_name": "KeralaXpress Delivery",
        "sender_company": "KeralaXpress",
        "sender_add_line_1": "Kochi Head Office",
        "sender_city": "ERNAKULAM",
        "sender_state": "KERALA",
        "sender_pincode": "682001",
        "sender_mobile_no": "9400662693",
        "receiver_name": "Test Receiver",
        "receiver_company": "Test Company",
        "receiver_add_line_1": "New Delhi GPO",
        "receiver_city": "CENTRAL DELHI",
        "receiver_state": "DELHI",
        "receiver_pincode": "110001",
        "receiver_mobile_no": "9876543210",
        # Returns must reach the seller, not KeralaXpress, so the alternate
        # address carries the seller.
        "alt_address_flag": "TRUE",
        "alt_addressee_name": "Test Seller",
        "alt_company_name": "Test Seller Shop",
        "alt_address_line1": "Seller Street",
        "alt_city": "ERNAKULAM",
        "alt_state": "KERALA",
        "alt_pincode": "682002",
        "alt_alternate_mobile_no": "9400662694",
        # Pickup happens at the seller's premises.
        "pickup_address_flag": "TRUE",
        "pickup_addressee_name": "Test Seller",
        "pickup_company_name": "Test Seller Shop",
        "pickup_address_line1": "Seller Street",
        "pickup_city": "ERNAKULAM",
        "pickup_state": "KERALA",
        "pickup_pincode": "682002",
        "pickup_mobile_no": "9400662694",
        "pickup_schedule_slot": "10:00-13:00",
        "pickup_schedule_date": "09/08/2026 10:00:00 AM",
        "ack": "FALSE",
        "reg": "FALSE",
        "otp": "FALSE",
    }


def check_pickup(token):
    print("=" * 72)
    print("PICKUP-MODE BOOKING VALIDATION")
    print("=" * 72)

    variants = [
        ("full pickup payload", {}),
        ("missing pickup_schedule_slot", {"pickup_schedule_slot": ""}),
        ("invalid slot 16:00-19:00",
         {"pickup_schedule_slot": "16:00-19:00"}),
        ("date as DD/MM/YYYY instead of MM/DD/YYYY",
         {"pickup_schedule_date": "08/09/2026 10:00:00 AM"}),
        ("date as ISO 8601",
         {"pickup_schedule_date": "2026-09-08T10:00:00"}),
        ("pickup date in the past",
         {"pickup_schedule_date": "01/05/2026 10:00:00 AM"}),
    ]

    for label, override in variants:
        art = pickup_article()
        art.update(override)
        status, raw = call("POST", "/process-articles/%s" % TEST_CUSTOMER,
                           token=token, body={"articles": [art]})
        try:
            payload = json.loads(raw)
        except ValueError:
            print("%-44s HTTP %s  %s" % (label, status, raw[:110]))
            continue
        if not payload.get("success"):
            print("%-44s HTTP %s  REJECTED: %s"
                  % (label, status,
                     payload.get("message") or payload.get("error")))
            continue
        errs = []
        for art_err in payload.get("error_articles") or []:
            errs.extend(art_err.get("errors") or [])
        # "Barcode number is required" is our own deliberate omission.
        other = [e for e in errs if "arcode" not in e]
        print("%-44s HTTP %s" % (label, status))
        for e in other:
            print("      %s" % e)
        if not other:
            print("      (no errors beyond the omitted barcode)")
    print()


def main():
    token = login()
    if not token:
        print("set INDIAPOST_USERNAME and INDIAPOST_PASSWORD")
        return 2
    print("logged in\n")
    check_coverage(token)
    check_pickup(token)
    print("No article carried a barcode, so nothing was booked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
