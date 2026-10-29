"""The behaviours a client is actually paying for.

Run: pytest -q
"""

import json
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A receiver with its own throwaway database, in dry-run mode."""
    monkeypatch.setenv("WEBHOOK_SYNC_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("WEBHOOK_DRY_RUN", "1")

    # Import after the environment is set, so config picks it up.
    import importlib

    from app import config, destinations, main, security, store
    for module in (config, store, destinations, main):
        importlib.reload(module)
    importlib.reload(security)

    destinations.DRY_RUN_LOG = tmp_path / "dry_run.log"

    with TestClient(main.app) as test_client:
        test_client.security = security
        test_client.store = store
        test_client.config = config
        yield test_client


def send(client, payload, timestamp=None, secret="test-secret"):
    raw = json.dumps(payload).encode()
    timestamp = timestamp if timestamp is not None else int(time.time())
    header = client.security.sign(raw, timestamp, secret)
    return client.post(
        "/webhooks/billing",
        content=raw,
        headers={"Content-Type": "application/json", "X-Signature": header},
    )


PAYMENT = {
    "id": "evt_test_001",
    "type": "payment.succeeded",
    "customer_email": "ana@example.com",
    "amount_minor": 24900,
    "currency": "EUR",
}


def test_valid_event_is_accepted_and_queued(client):
    response = send(client, PAYMENT)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert set(body["queued"]) == {"slack", "sheets"}


def test_duplicate_event_is_not_queued_twice(client):
    assert send(client, PAYMENT).json()["status"] == "accepted"
    second = send(client, PAYMENT).json()
    assert second["status"] == "duplicate"
    assert second["queued"] == []

    # One event, two deliveries -- not two events and four deliveries.
    with client.store.connect() as conn:
        events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        deliveries = conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries"
        ).fetchone()["n"]
    assert (events, deliveries) == (1, 2)


def test_repeat_deliveries_are_counted(client):
    # Providers retry. Keeping the count is what lets the operations page
    # show the dedupe key earning its keep.
    for _ in range(3):
        send(client, PAYMENT)

    with client.store.connect() as conn:
        assert client.store.duplicate_count(conn) == 2


def test_operations_page_renders_with_data(client):
    send(client, PAYMENT)
    client.post("/admin/drain")

    response = client.get("/")
    assert response.status_code == 200
    assert "Webhook Operations" in response.text
    assert PAYMENT["id"] in response.text


def test_operations_page_renders_when_empty(client):
    # An empty database must not 500 the page a client has bookmarked.
    response = client.get("/")
    assert response.status_code == 200
    assert "Nothing received yet" in response.text


def test_bad_signature_is_rejected(client):
    response = send(client, PAYMENT, secret="wrong-secret")
    assert response.status_code == 401


def test_missing_signature_is_rejected(client):
    response = client.post("/webhooks/billing", json=PAYMENT)
    assert response.status_code == 401


def test_stale_signature_is_rejected(client):
    old = int(time.time()) - 3600
    response = send(client, PAYMENT, timestamp=old)
    assert response.status_code == 401
    assert "off" in response.json()["detail"]


def test_unrouted_type_is_acknowledged_not_errored(client):
    response = send(client, {**PAYMENT, "id": "evt_x", "type": "invoice.draft"})
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_malformed_json_is_rejected(client):
    raw = b"{not json"
    header = client.security.sign(raw, int(time.time()), "test-secret")
    response = client.post(
        "/webhooks/billing", content=raw,
        headers={"Content-Type": "application/json", "X-Signature": header},
    )
    assert response.status_code == 400


def test_drain_delivers_queued_events(client):
    send(client, PAYMENT)
    result = client.post("/admin/drain").json()
    assert result == {"delivered": 2, "retried": 0, "dead": 0}

    health = client.get("/healthz").json()
    assert health["status"] == "ok"
    assert health["deliveries"] == {"delivered": 2}


def test_failed_delivery_retries_then_dies(client, monkeypatch):
    from app import destinations, store, worker

    def always_fails(event_type, payload):
        raise destinations.DeliveryError("destination is down")

    monkeypatch.setitem(destinations.REGISTRY, "slack", always_fails)
    monkeypatch.setitem(destinations.REGISTRY, "sheets", always_fails)
    monkeypatch.setattr(store, "BASE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(client.config, "MAX_ATTEMPTS", 2)

    send(client, PAYMENT)

    first = worker.process_due()
    assert first == (0, 2, 0)          # both retried, neither dead yet

    second = worker.process_due()
    assert second == (0, 0, 2)         # attempt cap reached, parked as dead

    health = client.get("/healthz").json()
    assert health["status"] == "degraded"
    assert health["deliveries"] == {"dead": 2}


def test_requeue_revives_dead_deliveries(client, monkeypatch):
    from app import destinations, store, worker

    monkeypatch.setitem(
        destinations.REGISTRY, "slack",
        lambda *_: (_ for _ in ()).throw(destinations.DeliveryError("down")),
    )
    monkeypatch.setitem(
        destinations.REGISTRY, "sheets",
        lambda *_: (_ for _ in ()).throw(destinations.DeliveryError("down")),
    )
    monkeypatch.setattr(store, "BASE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(client.config, "MAX_ATTEMPTS", 1)

    send(client, PAYMENT)
    worker.process_due()
    assert client.get("/healthz").json()["deliveries"] == {"dead": 2}

    assert client.post("/admin/requeue").json() == {"requeued": 2}
    assert client.get("/healthz").json()["deliveries"] == {"pending": 2}
