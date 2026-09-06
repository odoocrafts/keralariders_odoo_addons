#!/usr/bin/env python3
"""Follow-up probes for the Odoo India Post integration.

Answers the questions left open by indiapost_integration_probe.py:
  * does the booking validator enforce dimensions / weight at all?
  * what is the full 422 body for the undocumented L+W+H <= 300 rule?
  * are there pincodes with more than one bookable office (tie-break needed)?
  * what does the tracking ``event`` field actually contain in the wild?

Still omits ``barcode_no`` on every booking call, so no AWBs are consumed.
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
TEST_CUSTOMER_ID = "3000064781"
TEST_CONTRACT_ID = "41585456"


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
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, ssl.SSLError, socket.error) as exc:
        return 0, ("TRANSPORT ERROR: %r" % (exc,)).encode()


def as_json(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return None


def section(title):
    print("\n" + "=" * 76)
    print(title)
    print("=" * 76)


def login():
    _s, raw = call("POST", "/v1/access/login",
                   body={"username": os.environ.get("INDIAPOST_USERNAME"),
                         "password": os.environ.get("INDIAPOST_PASSWORD")})
    return ((as_json(raw) or {}).get("data") or {}).get("access_token")


def probe_tariff_errors(token):
    section("A. FULL 422 BODIES AND UNENFORCED LIMITS (tariff)")
    cases = [
        ("total 400cm", {"weight": 600, "length": 150, "width": 150, "height": 100}),
        ("total 301cm", {"weight": 600, "length": 151, "width": 100, "height": 50}),
        ("total 300cm exactly", {"weight": 600, "length": 150, "width": 100, "height": 50}),
        ("doc 250g height 5 (doc max 2)", {"weight": 250, "length": 30, "width": 21, "height": 5}),
        ("doc 250g 100x80x10 (way over doc box)",
         {"weight": 250, "length": 100, "width": 80, "height": 10}),
        ("doc 250g 1x1x1", {"weight": 250, "length": 1, "width": 1, "height": 1}),
        ("parcel 500g exactly 14x9x1", {"weight": 500, "length": 14, "width": 9, "height": 1}),
        ("parcel 499g 10x5x5 (doc, under parcel floor)",
         {"weight": 499, "length": 10, "width": 5, "height": 5}),
        ("weight 0", {"weight": 0, "length": 30, "width": 21, "height": 2}),
        ("weight 35001g", {"weight": 35001, "length": 50, "width": 40, "height": 30}),
        ("length 151 parcel", {"weight": 600, "length": 151, "width": 9, "height": 1}),
    ]
    for label, extra in cases:
        params = {"product-code": "SP", "source-pincode": "682001",
                  "destination-pincode": "110001"}
        params.update(extra)
        status, raw = call("GET", "/v1/speed-post/tariffs", token=token,
                           params=params)
        p = as_json(raw) or {}
        if p.get("success"):
            print("%-44s HTTP %s OK product=%s chg_wt=%s total=%s"
                  % (label, status, p.get("product_code"),
                     p.get("chargeable_weight"), p.get("final_amount")))
        else:
            print("%-44s HTTP %s ERR %s"
                  % (label, status, json.dumps(p.get("error") or p.get("message"))))


def article(**overrides):
    payload = {
        "bulk_customer_id": TEST_CUSTOMER_ID,
        "contract_id": TEST_CONTRACT_ID,
        "pickup_or_dropoff": "PICKUP",
        "pickup_dropoff_office_id": "22360020",
        "article_type": "SP",
        "physical_weight": 1500,
        "shape_of_article": "NROL",
        "length": 30,
        "breadth_diameter": 20,
        "height": 15,
        "bulk_reference": "PROBE2",
        "sender_name": "KeralaXpress Delivery",
        "sender_company": "KeralaXpress Logistics Private Limited",
        "sender_add_line_1": "Door 12/345, MG Road, Kochi",
        "sender_city": "Ernakulam",
        "sender_state": "Kerala",
        "sender_pincode": "682001",
        "sender_mobile_no": "9400662693",
        "receiver_name": "Test Receiver Name",
        "receiver_company": "Test Receiver Company",
        "receiver_add_line_1": "Connaught Place, Block A, New Delhi",
        "receiver_city": "New Delhi",
        "receiver_state": "Delhi",
        "receiver_pincode": "110001",
        "receiver_mobile_no": "9876543210",
        "pickup_address_flag": "TRUE",
        "pickup_addressee_name": "Kochi Seller Name",
        "pickup_company_name": "Kochi Seller Traders",
        "pickup_address_line1": "Shop 4, Broadway, Ernakulam",
        "pickup_city": "Ernakulam",
        "pickup_state": "Kerala",
        "pickup_pincode": "682001",
        "pickup_mobile_no": "9847012345",
        "pickup_schedule_slot": "10:00-13:00",
        "pickup_schedule_date": "12/15/2026 10:00:00 AM",
        "alt_address_flag": "TRUE",
        "alt_addressee_name": "Kochi Seller Name",
        "alt_company_name": "Kochi Seller Traders",
        "alt_address_line1": "Shop 4, Broadway, Ernakulam",
        "alt_city": "Ernakulam",
        "alt_state": "Kerala",
        "alt_pincode": "682001",
        "alt_alternate_mobile_no": "9847012345",
    }
    payload.update(overrides)
    return payload


def book(label, token, articles):
    status, raw = call("POST", "/process-articles/%s" % TEST_CUSTOMER_ID,
                       token=token, body={"articles": articles})
    p = as_json(raw)
    errs = []
    if isinstance(p, dict):
        for art in (p.get("error_articles") or []):
            errs.extend(art.get("errors") or [])
    # Strip the two errors we always expect from omitting barcode_no so only
    # the interesting validation shows up.
    extra = [e for e in errs
             if "Barcode number is required" not in e
             and "Text value is required for data processing" not in e]
    print("%-46s HTTP %s  extra errors: %s"
          % (label, status, json.dumps(extra) if extra else "none"))


def probe_booking_validation(token):
    section("B. WHAT DOES THE BOOKING VALIDATOR ACTUALLY ENFORCE?")
    print("(the two barcode-related errors are filtered out of every line)\n")
    book("baseline valid parcel", token, [article()])
    book("small heavy 600g at 10x5x5", token,
         [article(physical_weight=600, length=10, breadth_diameter=5, height=5)])
    book("total dims 400cm", token,
         [article(physical_weight=600, length=150, breadth_diameter=150, height=100)])
    book("weight 0", token, [article(physical_weight=0)])
    book("weight 50000g", token, [article(physical_weight=50000)])
    book("decimal weight 1500.5", token, [article(physical_weight=1500.5)])
    book("DOC shape but 1500g", token, [article(shape_of_article="DOC")])
    book("NROL shape but 250g", token,
         [article(physical_weight=250, length=30, breadth_diameter=21, height=2)])
    book("bad shape code XXXX", token, [article(shape_of_article="XXXX")])
    book("5-digit pincode", token, [article(receiver_pincode="11000")])
    book("nonexistent pincode 999999", token, [article(receiver_pincode="999999")])
    book("bad office id", token, [article(pickup_dropoff_office_id="00000000")])
    book("pickup_or_dropoff = DROPOFF", token,
         [article(pickup_or_dropoff="DROPOFF", pickup_address_flag="FALSE")])
    book("pickup date in the past", token,
         [article(pickup_schedule_date="01/15/2020 10:00:00 AM")])
    book("pickup date ISO format", token,
         [article(pickup_schedule_date="2026-12-15T10:00:00")])
    book("pickup date DD/MM/YYYY", token,
         [article(pickup_schedule_date="15/12/2026 10:00:00 AM")])
    book("missing sender_name", token, [article(sender_name="")])
    book("missing receiver_add_line_1", token, [article(receiver_add_line_1="")])
    book("address lines totalling 300 chars", token,
         [article(receiver_add_line_1="A" * 80, receiver_add_line_2="B" * 80,
                  receiver_add_line_3="C" * 80, sender_add_line_1="D" * 60)])
    book("name 200 chars", token, [article(receiver_name="Z" * 200)])
    book("delivery_instruction ND + slot", token,
         [article(delivery_instruction="ND", delivery_slot="9am-2pm")])
    book("instruction_rts RTS", token, [article(instruction_rts="RTS")])
    book("insurance DOP 5000", token,
         [article(insurance_type="DOP", value_of_insurance=5000)])
    book("ack/reg/otp TRUE", token,
         [article(ack="TRUE", reg="TRUE", otp="TRUE")])
    book("bulk_reference 60 chars", token, [article(bulk_reference="R" * 60)])
    book("unknown extra key", token, [article(keralaxpress_note="hello")])


OFFICE_TYPE_RANK = {"HPO": 0, "GPO": 0, "SPO": 1, "MDG": 2, "IDC": 3}


def probe_multi_office(token):
    section("C. PINCODES WITH MORE THAN ONE BOOKABLE OFFICE")
    pincodes = ["682001", "682002", "682011", "110001", "110006", "400001",
                "560001", "600001", "700001", "500001", "695001", "673001",
                "670001", "688001", "691001", "686001", "678001", "676505",
                "685603", "689645", "671121", "673121", "680001", "302001"]
    multi = 0
    for pincode in pincodes:
        status, raw = call("GET", "/v1/pincode-search", token=token,
                           params={"pincode": pincode, "office-type": "post"})
        p = as_json(raw) or {}
        recs = [r for r in (p.get("data") or []) if isinstance(r, dict)]
        bookable = [r for r in recs
                    if r.get("delivery_office_flag")
                    and (r.get("office_type_code") or "").upper() != "BPO"]
        bookable.sort(key=lambda r: (
            0 if r.get("is_rolled_out") else 1,
            OFFICE_TYPE_RANK.get((r.get("office_type_code") or "").upper(), 9),
            str(r.get("office_name") or ""),
            str(r.get("office_id") or ""),
        ))
        flag = ""
        if len(bookable) > 1:
            multi += 1
            flag = "  <-- TIE-BREAK USED: " + str(
                [(o.get("office_id"), o.get("office_type_code"),
                  o.get("office_name"), o.get("is_rolled_out"))
                 for o in bookable[:4]])
        print("%s returned=%-3s bookable=%-2s chosen=%s %s (%s rolled_out=%s)%s"
              % (pincode, p.get("returned_records_count"), len(bookable),
                 bookable and bookable[0].get("office_id"),
                 bookable and bookable[0].get("office_name"),
                 bookable and bookable[0].get("office_type_code"),
                 bookable and bookable[0].get("is_rolled_out"), flag))
    print("\n%d of %d pincodes returned more than one bookable office"
          % (multi, len(pincodes)))
    # Also check the no-office-type variant, which the addon uses as a retry.
    status, raw = call("GET", "/v1/pincode-search", token=token,
                       params={"pincode": "682001"})
    p = as_json(raw) or {}
    print("682001 without office-type -> returned=%s"
          % p.get("returned_records_count"))


def probe_tracking_events(token):
    section("D. REAL TRACKING EVENT STRINGS (for the status mapper)")
    # A spread of live barcodes from the vendor documents plus prefix variants.
    bulk = ["RK775227016IN", "EY011867595IN", "EB126023474IN",
            "EE123456789IN", "ED123456789IN", "CG123456789IN"]
    status, raw = call("POST", "/v1/tracking/bulk", token=token,
                       body={"bulk": bulk})
    p = as_json(raw) or {}
    print("HTTP %s records=%s" % (status, len(p.get("data") or [])))
    seen = {}
    for rec in (p.get("data") or []):
        bd = rec.get("booking_details") or {}
        events = rec.get("tracking_details") or []
        del_status = rec.get("del_status")
        if isinstance(del_status, dict):
            del_status = del_status.get("del_status")
        print("\n  booking_details keys: %s" % sorted(bd.keys()))
        print("  booking_details: %s" % json.dumps(bd)[:300])
        print("  del_status=%r events=%d" % (del_status, len(events)))
        for ev in events:
            print("     %s %s | %-28s | %s"
                  % (ev.get("date"), ev.get("time"),
                     ev.get("office"), ev.get("event")))
            seen[ev.get("event")] = seen.get(ev.get("event"), 0) + 1
    print("\ndistinct event strings observed:")
    for text, count in sorted(seen.items(), key=lambda kv: -kv[1]):
        print("  %3dx  %r" % (count, text))


def main():
    if not os.environ.get("INDIAPOST_USERNAME"):
        print("set INDIAPOST_USERNAME / INDIAPOST_PASSWORD")
        return 2
    token = login()
    if not token:
        print("login failed")
        return 2
    probe_tariff_errors(token)
    probe_booking_validation(token)
    probe_multi_office(token)
    probe_tracking_events(token)
    print("\nDone. barcode_no omitted throughout; 0 AWBs consumed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
