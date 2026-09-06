#!/usr/bin/env python3
"""Probe the two India Post contract ids issued to KeralaXpress.

India Post issued one contract per product: Speed Post and Business Parcel.
This script answers, against the UAT sandbox, which product code each contract
is bound to and whether contract validation now passes.

No article carries barcode_no, so booking is guaranteed to be rejected on the
missing barcode and no AWB from the allotted range is consumed. What the error
list reveals is whether the customer id / contract id / article type triple was
accepted before the per-field validation ran.

    export INDIAPOST_USERNAME=... INDIAPOST_PASSWORD=...
    python3 indiapost_contract_probe.py
"""

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("INDIAPOST_BASE_URL", "https://test.cept.gov.in/beextcustomer")
TIMEOUT = 60

SP_CONTRACT = os.environ.get("INDIAPOST_SP_CONTRACT", "41124829")
BP_CONTRACT = os.environ.get("INDIAPOST_BP_CONTRACT", "41664688")


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
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, ssl.SSLError, socket.error) as exc:
        return 0, "TRANSPORT ERROR: %r" % (exc,)


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def login(username, password):
    status, raw = call("POST", "/v1/access/login",
                       body={"username": username, "password": password})
    if status != 200:
        print("login failed HTTP %s: %s" % (status, raw[:400]))
        return None
    return (json.loads(raw).get("data") or {}).get("access_token")


def article(customer_id, contract_id, article_type, weight=250,
            dims=(30, 21, 2), shape="DOC", with_barcode=None):
    payload = {
        "bulk_customer_id": str(customer_id),
        "contract_id": str(contract_id),
        "pickup_or_dropoff": "DROPOFF",
        "pickup_dropoff_office_id": 22360020,   # Kochi HO
        "article_type": article_type,
        "physical_weight": weight,
        "shape_of_article": shape,
        "length": dims[0],
        "breadth_diameter": dims[1],
        "height": dims[2],
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
        "alt_address_flag": "FALSE",
        "pickup_address_flag": "FALSE",
        "drop_off_pincode": "682001",
        "ack": "FALSE",
        "reg": "FALSE",
        "otp": "FALSE",
    }
    if with_barcode:
        payload["barcode_no"] = with_barcode
    return payload


def probe_tariffs(token):
    section("TARIFF: which product codes does the endpoint price?")
    base = {"weight": 250, "source-pincode": "682001",
            "destination-pincode": "110001",
            "length": 30, "width": 21, "height": 2}
    for code in ("SP", "BP", "PP", "BPP", "EP", "RP", "NDD", "SPNDD"):
        params = dict(base, **{"product-code": code})
        status, raw = call("GET", "/v1/speed-post/tariffs",
                           token=token, params=params)
        try:
            p = json.loads(raw)
        except ValueError:
            print("product-code=%-6s HTTP %s  %s" % (code, status, raw[:160]))
            continue
        if isinstance(p, dict) and p.get("success"):
            print("product-code=%-6s HTTP %s  product=%s base=%s vas=%s "
                  "total=%s charged=%s"
                  % (code, status, p.get("product_code"), p.get("base_tariff"),
                     p.get("vas_charges"), p.get("final_amount"),
                     p.get("chargeable_weight")))
        else:
            print("product-code=%-6s HTTP %s  %s"
                  % (code, status,
                     (p.get("error") or p.get("message"))
                     if isinstance(p, dict) else str(p)[:160]))

    section("TARIFF: heavy parcel per product code (2 kg, 30x20x15)")
    heavy = {"weight": 2000, "source-pincode": "682001",
             "destination-pincode": "110001",
             "length": 30, "width": 20, "height": 15}
    for code in ("SP", "BP", "PP"):
        status, raw = call("GET", "/v1/speed-post/tariffs", token=token,
                           params=dict(heavy, **{"product-code": code}))
        try:
            p = json.loads(raw)
        except ValueError:
            print("product-code=%-4s HTTP %s  %s" % (code, status, raw[:160]))
            continue
        if isinstance(p, dict) and p.get("success"):
            print("product-code=%-4s HTTP %s  product=%s base=%s total=%s"
                  % (code, status, p.get("product_code"),
                     p.get("base_tariff"), p.get("final_amount")))
        else:
            print("product-code=%-4s HTTP %s  %s"
                  % (code, status, (p.get("error") or p.get("message"))
                     if isinstance(p, dict) else str(p)[:160]))

    section("TARIFF: does a separate business-parcel path exist?")
    for path in ("/v1/business-parcel/tariffs", "/v1/parcel/tariffs",
                 "/v1/business-parcel/tariff", "/v1/tariffs"):
        status, raw = call("GET", path, token=token,
                           params=dict(base, **{"product-code": "BP"}))
        print("%-32s HTTP %s  %s" % (path, status, raw[:140].replace("\n", " ")))


def show_booking(label, customer_id, body):
    print("-" * 72)
    print(label)
    status, raw = call("POST", "/process-articles/%s" % customer_id,
                       token=TOKEN, body=body)
    print("HTTP %s" % status)
    try:
        payload = json.loads(raw)
    except ValueError:
        print(raw[:600])
        return None
    if not payload.get("success"):
        print("top-level rejection: %s" % json.dumps(payload)[:800])
        return payload
    for art in payload.get("error_articles") or []:
        print("article errors: %s" % json.dumps(art.get("errors")))
    for art in payload.get("success_articles") or []:
        print("BOOKED: %s" % json.dumps(art)[:600])
    print("summary: %s" % json.dumps(payload.get("summary") or {}))
    print("batch: %s correlation: %s" % (payload.get("batch_id"),
                                         payload.get("correlation_id")))
    return payload



def probe_bp_tariff_matrix(token):
    section("TARIFF: is any Business Parcel tariff loaded in the sandbox?")
    lanes = [
        ("local, same pincode", "682001", "682001"),
        ("same city", "682001", "682002"),
        ("within Kerala", "682001", "695001"),
        ("metro to metro", "682001", "110001"),
    ]
    for label, src, dst in lanes:
        for weight in (250, 2000, 5000, 20000):
            params = {"product-code": "BP", "weight": weight,
                      "source-pincode": src, "destination-pincode": dst,
                      "length": 30, "width": 20, "height": 15}
            status, raw = call("GET", "/v1/speed-post/tariffs",
                               token=token, params=params)
            try:
                p = json.loads(raw)
            except ValueError:
                print("%-20s %6dg HTTP %s %s" % (label, weight, status, raw[:120]))
                continue
            if isinstance(p, dict) and p.get("success"):
                print("%-20s %6dg HTTP %s product=%s base=%s total=%s"
                      % (label, weight, status, p.get("product_code"),
                         p.get("base_tariff"), p.get("final_amount")))
            else:
                print("%-20s %6dg HTTP %s %s"
                      % (label, weight, status,
                         (p.get("error") or p.get("message"))
                         if isinstance(p, dict) else str(p)[:120]))


def probe_contract_taxonomy(customer_id):
    section("BOOKING VALIDATION: how the validator talks about contracts")
    # Distinguishing "this contract is not ours / does not exist" from "this
    # contract exists but India Post has not attached a service type to it" is
    # the whole point: only the second is a wait-for-India-Post state.
    cases = [
        ("our Speed Post contract", SP_CONTRACT, "SP"),
        ("our Business Parcel contract", BP_CONTRACT, "BP"),
        ("nonexistent contract 99999999", "99999999", "SP"),
        ("document sample SP contract 41585456", "41585456", "SP"),
        ("document sample BP contract 41367422", "41367422", "BP"),
        ("malformed contract ABCDEFGH", "ABCDEFGH", "SP"),
    ]
    for label, contract, atype in cases:
        show_booking("%s (%s, article_type %s)" % (label, contract, atype),
                     customer_id,
                     {"articles": [article(customer_id, contract, atype)]})



def attempt_real_booking(customer_id):
    """Opt-in: send one article that *could* book, to prove the outcome.

    Contract validation short-circuits ahead of every other check, so while the
    contract is unusable this consumes nothing. Guarded by an environment
    variable anyway, because the day the contract works this call burns an AWB
    from the allotted UAT range. The barcode used is deliberately the last
    serial of the range, so an unexpected success cannot collide with the
    serial the Odoo allocator is about to hand out.
    """
    barcode = os.environ.get("INDIAPOST_TEST_BARCODE", "ET214340000IN")
    section("REAL BOOKING ATTEMPT with barcode %s" % barcode)
    for label, contract, atype, weight, dims, shape in (
        ("Speed Post contract %s" % SP_CONTRACT, SP_CONTRACT, "SP",
         250, (30, 21, 2), "DOC"),
    ):
        show_booking("%s + article_type %s, barcode %s"
                     % (label, atype, barcode), customer_id,
                     {"articles": [article(customer_id, contract, atype,
                                           weight, dims, shape,
                                           with_barcode=barcode)]})


TOKEN = None


def main():
    global TOKEN
    username = os.environ.get("INDIAPOST_USERNAME")
    password = os.environ.get("INDIAPOST_PASSWORD")
    customer_id = os.environ.get("INDIAPOST_CUSTOMER_ID", username)
    if not username or not password:
        print("set INDIAPOST_USERNAME and INDIAPOST_PASSWORD")
        return 2
    TOKEN = login(username, password)
    if not TOKEN:
        return 2
    print("logged in as %s against %s" % (username, BASE))
    print("speed post contract %s, business parcel contract %s"
          % (SP_CONTRACT, BP_CONTRACT))

    probe_tariffs(TOKEN)

    section("BOOKING VALIDATION (no barcode_no, nothing can be booked)")
    combos = [
        ("Speed Post contract %s + article_type SP" % SP_CONTRACT,
         SP_CONTRACT, "SP", 250, (30, 21, 2), "DOC"),
        ("Business Parcel contract %s + article_type BP" % BP_CONTRACT,
         BP_CONTRACT, "BP", 2000, (30, 20, 15), "NROL"),
        ("Business Parcel contract %s + article_type PP" % BP_CONTRACT,
         BP_CONTRACT, "PP", 2000, (30, 20, 15), "NROL"),
        ("cross: Speed Post contract %s + article_type BP" % SP_CONTRACT,
         SP_CONTRACT, "BP", 2000, (30, 20, 15), "NROL"),
        ("cross: Business Parcel contract %s + article_type SP" % BP_CONTRACT,
         BP_CONTRACT, "SP", 250, (30, 21, 2), "DOC"),
        ("no contract id at all + article_type SP",
         "", "SP", 250, (30, 21, 2), "DOC"),
    ]
    for label, contract, atype, weight, dims, shape in combos:
        show_booking(label, customer_id, {"articles": [
            article(customer_id, contract, atype, weight, dims, shape)]})

    probe_contract_taxonomy(customer_id)
    probe_bp_tariff_matrix(TOKEN)

    if os.environ.get("INDIAPOST_ATTEMPT_BOOKING"):
        attempt_real_booking(customer_id)
        print("\nA barcode-carrying article was sent: check the result above "
              "for whether an AWB was consumed.")
        return 0

    print("\nNo article carried barcode_no, so no AWB was consumed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
