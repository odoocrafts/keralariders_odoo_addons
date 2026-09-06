#!/usr/bin/env python3
"""Probe the India Post booking validator without consuming a barcode.

Booking requires both bulk_customer_id and contract_id. Our sandbox
registration supplied a customer id but no contract id, so this script checks
which combinations the validator accepts.

Every article deliberately omits barcode_no. That field is mandatory, so each
article is guaranteed to be rejected and no AWB from our allotted range is
consumed -- but the error list still reveals whether the customer id and
contract id themselves were accepted.

    export INDIAPOST_USERNAME=... INDIAPOST_PASSWORD=...
    python3 indiapost_booking_probe.py
"""

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request

BASE = "https://test.cept.gov.in/beextcustomer"
TIMEOUT = 45

# Contract ids published in the approach document against test customer
# 3000064781. We only care about the Speed Post one, but try each to see
# whether any is bound to our own customer id.
DOC_CONTRACTS = [
    ("41585456", "SP"),
    ("41367422", "BP"),
]


def call(method, path, token=None, body=None):
    req = urllib.request.Request(
        BASE + path,
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


def article(customer_id, contract_id):
    """A minimal Speed Post dropoff article, intentionally missing barcode_no."""
    return {
        "bulk_customer_id": str(customer_id),
        "contract_id": str(contract_id),
        # barcode_no deliberately omitted -> guaranteed rejection.
        "pickup_or_dropoff": "DROPOFF",
        "pickup_dropoff_office_id": 22360020,   # Kochi HO, resolved from pincode API
        "article_type": "SP",
        "physical_weight": 250,
        "shape_of_article": "DOC",
        "length": 30,
        "breadth_diameter": 21,
        "height": 2,
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


def main():
    username = os.environ.get("INDIAPOST_USERNAME")
    password = os.environ.get("INDIAPOST_PASSWORD")
    if not username or not password:
        print("set INDIAPOST_USERNAME and INDIAPOST_PASSWORD")
        return 2

    status, raw = call("POST", "/v1/access/login",
                       body={"username": username, "password": password})
    if status != 200:
        print("login failed: HTTP %s\n%s" % (status, raw[:500]))
        return 2
    token = (json.loads(raw).get("data") or {}).get("access_token")
    if not token:
        print("no token in login response")
        return 2
    print("logged in as %s\n" % username)

    for contract_id, label in DOC_CONTRACTS:
        for customer_id in (username, "3000064781"):
            print("=" * 72)
            print("customer_id=%s  contract_id=%s (%s)"
                  % (customer_id, contract_id, label))
            status, raw = call(
                "POST", "/process-articles/%s" % customer_id, token=token,
                body={"articles": [article(customer_id, contract_id)]},
            )
            print("HTTP %s" % status)
            try:
                payload = json.loads(raw)
            except ValueError:
                print(raw[:600])
                continue

            # A top-level failure usually means the customer/contract pair was
            # rejected outright. Per-article errors mean the pair was accepted
            # and only the article content failed.
            if not payload.get("success"):
                print("top-level rejection: %s"
                      % payload.get("message") or payload.get("error"))
                print(json.dumps(payload, indent=2)[:700])
            else:
                for art in payload.get("error_articles") or []:
                    print("article errors: %s" % json.dumps(art.get("errors")))
                summary = payload.get("summary") or {}
                print("summary: %s" % json.dumps(summary))
            print()

    print("No article carried a barcode, so nothing was booked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
