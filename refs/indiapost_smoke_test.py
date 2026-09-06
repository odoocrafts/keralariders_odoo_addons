#!/usr/bin/env python3
"""India Post UAT connectivity smoke test.

Run this from the whitelisted server. Uses only the standard library so it can
run under the Odoo virtualenv or bare system python3.

Credentials come from $INDIAPOST_USERNAME and $INDIAPOST_PASSWORD when set,
which keeps them out of shell history; otherwise the documented sandbox sample
account is used.

    export INDIAPOST_USERNAME=...
    read -rs INDIAPOST_PASSWORD && export INDIAPOST_PASSWORD
    python3 indiapost_smoke_test.py
    python3 indiapost_smoke_test.py --pincode 682001 --dest-pincode 110001

Read-only: performs login, pincode search and tariff lookups only. It never
calls the booking endpoint, so no barcodes from our allotted AWB range are
consumed.
"""

import argparse
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


def request(method, path, token=None, params=None, body=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, raw, url
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), url
    except (urllib.error.URLError, ssl.SSLError, socket.error) as exc:
        return 0, "TRANSPORT ERROR: %r" % (exc,), url


def records(raw):
    """Pull the list of records out of a response.

    The document shows pincode-search returning a bare array, but the live
    sandbox wraps it as {"data": [...]}. Accept either.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
    return []


def show(label, status, raw, url, limit=1800):
    print("\n" + "=" * 72)
    print("%s  ->  HTTP %s" % (label, status or "no response"))
    print(url)
    print("-" * 72)
    try:
        print(json.dumps(json.loads(raw), indent=2)[:limit])
    except (ValueError, TypeError):
        print(raw[:limit])


def main():
    ap = argparse.ArgumentParser()
    # Prefer the environment so credentials stay out of shell history. Falls
    # back to the sample account documented for the sandbox.
    ap.add_argument("--username",
                    default=os.environ.get("INDIAPOST_USERNAME", "9999999999"),
                    help="UAT username; defaults to $INDIAPOST_USERNAME")
    ap.add_argument("--password",
                    default=os.environ.get("INDIAPOST_PASSWORD", "Dop@1234"),
                    help="UAT password; defaults to $INDIAPOST_PASSWORD")
    ap.add_argument("--pincode", default="682001",
                    help="origin pincode to resolve post offices for")
    ap.add_argument("--dest-pincode", default="110001")
    args = ap.parse_args()

    print("India Post UAT smoke test")
    print("target: %s" % BASE)

    # 1. Reachability. A transport error here means this host is not whitelisted.
    status, raw, url = request(
        "POST", "/v1/access/login",
        body={"username": args.username, "password": args.password},
    )
    show("1. LOGIN", status, raw, url)

    if status == 0:
        print("\nThis host cannot reach the API at all (connection refused or "
              "reset). Source IP is most likely not whitelisted.")
        return 2

    token = None
    try:
        payload = json.loads(raw)
        token = (payload.get("data") or {}).get("access_token") or None
    except ValueError:
        pass

    if not token:
        print("\nNo access_token in the response. The credentials above are "
              "not valid for this sandbox; request credentials for "
              "KeralaXpress from integrations.cept@indiapost.gov.in.")
        return 1

    print("\nToken acquired (%d chars, expires in %ss)."
          % (len(token), (json.loads(raw).get("data") or {}).get("expires_in")))

    # 2. Pincode search. Also tells us which office ids are bookable.
    status, raw, url = request(
        "GET", "/v1/pincode-search", token=token,
        params={"pincode": args.pincode, "office-type": "post"},
    )
    show("2. PINCODE SEARCH (origin %s)" % args.pincode, status, raw, url)

    source_office = None
    offices = records(raw)
    # Documented rule: delivery_office_flag true and office_type_code not BPO.
    bookable = [o for o in offices
                if o.get("delivery_office_flag")
                and o.get("office_type_code") != "BPO"]
    print("\nbookable offices: %d of %d returned" % (len(bookable), len(offices)))
    for o in bookable:
        print("  %s  %s  (%s)"
              % (o.get("office_id"), o.get("office_name"),
                 o.get("office_type_code")))
    if bookable:
        source_office = bookable[0].get("office_id")

    # 3. Speed Post tariff below 500g -> should resolve to SP_INLAND_DOC.
    status, raw, url = request(
        "GET", "/v1/speed-post/tariffs", token=token,
        params={"product-code": "SP", "weight": 250,
                "source-pincode": args.pincode,
                "destination-pincode": args.dest_pincode,
                "length": 30, "width": 21, "height": 2},
    )
    show("3. SPEED POST TARIFF, 250g (expect SP_INLAND_DOC)",
         status, raw, url)

    # 4. Speed Post tariff above 500g -> should resolve to SP_INLAND_PARCEL.
    status, raw, url = request(
        "GET", "/v1/speed-post/tariffs", token=token,
        params={"product-code": "SP", "weight": 1500,
                "source-pincode": args.pincode,
                "destination-pincode": args.dest_pincode,
                "length": 30, "width": 20, "height": 15},
    )
    show("4. SPEED POST TARIFF, 1500g (expect SP_INLAND_PARCEL)",
         status, raw, url)

    # 5. Small heavy item. Below the documented 14x9 parcel minimum, so this
    #    probes whether such articles are bookable at all.
    status, raw, url = request(
        "GET", "/v1/speed-post/tariffs", token=token,
        params={"product-code": "SP", "weight": 600,
                "source-pincode": args.pincode,
                "destination-pincode": args.dest_pincode,
                "length": 10, "width": 5, "height": 5},
    )
    show("5. SPEED POST TARIFF, 600g at 10x5x5cm (probes parcel dimension floor)",
         status, raw, url)

    if source_office:
        print("\nResolved a usable source office id for %s: %s"
              % (args.pincode, source_office))
    print("\nDone. No booking calls were made; no barcodes consumed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
