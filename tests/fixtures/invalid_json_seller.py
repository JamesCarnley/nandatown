#!/usr/bin/env python3
"""A seller whose acknowledgements hold values Town cannot store as JSON.

Standard-library Python, like examples/byoa_seller.py, except for two
mistakes a real agent can make with json.dumps defaults: a note key
holding an unpaired surrogate (a string sliced mid-character), and a
NaN "confidence" (json.dumps writes the non-standard NaN literal). The
town refuses both acknowledgements; everything else this seller does
is well formed.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

TOWN = os.environ["TOWN_URL"]
RUN = os.environ["RUN_ID"]
NAME = os.environ["NAME"]
TOKEN = os.environ["TOKEN"]
DEADLINE = time.time() + float(os.environ.get("DEADLINE", "45"))

session = None


def call(method, path, body=None):
    url = f"{TOWN}/runs/{RUN}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if session:
        req.add_header("X-Town-Session", session)
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status == 204:
            return None
        return json.loads(resp.read() or b"{}")


def main():
    global session
    session = call("POST", "/join", {"name": NAME, "token": TOKEN})["session"]
    while time.time() < DEADLINE:
        call("GET", "/inbox/notify?wait=0.4")
        claim = call("POST", "/inbox/claim")
        if claim is None:
            continue
        mid = claim["message_id"]
        body = claim["body"]
        total = body["quantity"] * body["unit_price_cents"]
        call("POST", "/messages",
             {"message_id": "r-" + mid.removeprefix("q-"),
              "to": claim["from"], "kind": "quote_response",
              "body": {"request_id": mid, "total_cents": total}})
        for note in ({"applied": True, "total_cents": total,
                      "sku\ud800": body["sku"]},
                     {"applied": True, "total_cents": total,
                      "confidence": float("nan")}):
            try:
                call("POST", "/inbox/ack",
                     {"message_id": mid, "fence": claim["fence"],
                      "status": "processed", "note": note})
            except urllib.error.HTTPError as exc:
                print(f"ack refused: {exc.code} {exc.read().decode()}",
                      file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
