#!/usr/bin/env python3
"""Send fixture events at a running receiver, correctly signed.

    python replay.py                         # every fixture, once
    python replay.py --twice                 # every fixture twice, to show dedupe
    python replay.py --url http://host:8000  # a deployed instance

This doubles as the thing you hand a client: "a webhook got lost, run this
against the fixture and watch it go through."
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests

from app import security

FIXTURES = Path(__file__).parent / "fixtures"


def post_fixture(base_url, path, secret):
    raw = path.read_bytes()
    header = security.sign(raw, int(time.time()), secret)
    response = requests.post(
        f"{base_url}/webhooks/billing",
        data=raw,
        headers={"Content-Type": "application/json", "X-Signature": header},
        timeout=10,
    )
    return response.status_code, response.json()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--secret", default=None,
                        help="defaults to WEBHOOK_SECRET from the environment")
    parser.add_argument("--twice", action="store_true",
                        help="send everything twice to demonstrate idempotency")
    parser.add_argument("--drain", action="store_true",
                        help="ask the receiver to process the queue afterwards")
    args = parser.parse_args()

    fixtures = sorted(FIXTURES.glob("*.json"))
    if not fixtures:
        print("no fixtures found", file=sys.stderr)
        return 1

    rounds = 2 if args.twice else 1
    for round_number in range(1, rounds + 1):
        if rounds > 1:
            print(f"--- round {round_number} ---")
        for path in fixtures:
            try:
                status, body = post_fixture(args.url, path, args.secret)
            except requests.RequestException as exc:
                print(f"{path.name}: connection failed: {exc}", file=sys.stderr)
                return 1
            print(f"{path.name:32} HTTP {status}  {json.dumps(body)}")

    if args.drain:
        response = requests.post(f"{args.url}/admin/drain", timeout=30)
        print("drain:", json.dumps(response.json()))

    return 0


if __name__ == "__main__":
    sys.exit(main())
