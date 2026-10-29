"""Where events go after they arrive.

Each destination is a function taking (event_type, payload) and either
returning normally or raising DeliveryError. The worker turns a raise into a
backed-off retry, so a destination never has to think about retries itself.

With config.DRY_RUN on (the default) nothing leaves the machine: deliveries
are appended to dry_run.log instead. That is what lets this project run with
no credentials.
"""

import json
import logging
from pathlib import Path

import requests

from app import config

log = logging.getLogger(__name__)

DRY_RUN_LOG = Path("dry_run.log")


class DeliveryError(RuntimeError):
    pass


def _money(payload):
    minor = payload.get("amount_minor")
    currency = payload.get("currency", "")
    if minor is None:
        return "unknown amount"
    return f"{minor / 100:.2f} {currency}".strip()


def _summary(event_type, payload):
    customer = payload.get("customer_email") or payload.get("customer") or "unknown"
    if event_type == "payment.succeeded":
        return f"Payment received: {_money(payload)} from {customer}"
    if event_type == "payment.failed":
        reason = payload.get("failure_reason", "no reason given")
        return f"Payment FAILED: {_money(payload)} from {customer} ({reason})"
    if event_type == "subscription.cancelled":
        return f"Subscription cancelled: {customer}"
    return f"{event_type}: {customer}"


def _dry_run(destination, body):
    with DRY_RUN_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"destination": destination, "body": body}) + "\n")
    log.info("[dry-run] %s <- %s", destination, body)


def _post(url, body, destination):
    try:
        response = requests.post(url, json=body, timeout=config.HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise DeliveryError(f"{destination}: {exc}") from exc

    # 5xx and 429 are worth retrying; a 4xx is a bug in our payload and will
    # fail identically forever, but the attempt cap stops it spinning.
    if response.status_code >= 400:
        raise DeliveryError(
            f"{destination}: HTTP {response.status_code} {response.text[:200]}"
        )


def send_slack(event_type, payload):
    body = {"text": _summary(event_type, payload)}
    if config.DRY_RUN or not config.SLACK_WEBHOOK_URL:
        _dry_run("slack", body)
        return
    _post(config.SLACK_WEBHOOK_URL, body, "slack")


def send_sheets(event_type, payload):
    body = {
        "row": [
            payload.get("occurred_at", ""),
            event_type,
            payload.get("customer_email", ""),
            f"{payload.get('amount_minor', 0) / 100:.2f}",
            payload.get("currency", ""),
            payload.get("id", ""),
        ]
    }
    if config.DRY_RUN or not config.SHEETS_WEBHOOK_URL:
        _dry_run("sheets", body)
        return
    _post(config.SHEETS_WEBHOOK_URL, body, "sheets")


REGISTRY = {
    "slack": send_slack,
    "sheets": send_sheets,
}


def deliver(destination, event_type, payload):
    handler = REGISTRY.get(destination)
    if handler is None:
        raise DeliveryError(f"unknown destination {destination!r}")
    handler(event_type, payload)
