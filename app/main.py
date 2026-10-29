"""Webhook receiver.

    uvicorn app.main:app --reload

The receiver does as little as possible: verify, persist, queue, return 200.
Delivery happens in the worker. A provider that doesn't get a fast 200 starts
retrying, and doing destination I/O inside the request is how you end up with
both a timeout and a duplicate.
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import config, security, store, worker

TEMPLATES = Path(__file__).parent / "templates"


@asynccontextmanager
async def lifespan(_app):
    store.init()
    yield


app = FastAPI(title="Webhook Integration Service", lifespan=lifespan)
jinja = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(["html"]),
)


@app.get("/", response_class=HTMLResponse)
def operations():
    """The page you leave open when an integration matters."""
    with store.connect() as conn:
        counts = store.counts(conn)
        events = store.events_with_delivery_state(conn)
        deliveries = store.recent_deliveries(conn)
        duplicates = store.duplicate_count(conn)

    template = jinja.get_template("operations.html")
    return template.render(
        events=[{
            "event_id": row["event_id"],
            "source": row["source"],
            "type": row["event_type"],
            "received_at": row["received_at"],
            "duplicates": row["duplicate_hits"] or 0,
            "total": row["total"] or 0,
            "delivered": row["delivered"] or 0,
            "pending": row["pending"] or 0,
            "dead": row["dead"] or 0,
            "attempts": row["attempts"] or 0,
        } for row in events],
        deliveries=[{
            "event_id": row["event_id"],
            "type": row["event_type"],
            "destination": row["destination"],
            "status": row["status"],
            "attempts": row["attempts"],
            "last_error": row["last_error"],
            "when": row["delivered_at"] or row["next_attempt_at"],
        } for row in deliveries],
        stats={
            "events": len(events),
            "duplicates": duplicates,
            "delivered": counts.get("delivered", 0),
            "pending": counts.get("pending", 0),
            "dead": counts.get("dead", 0),
        },
        degraded=bool(counts.get("dead")),
        dry_run=config.DRY_RUN,
        max_attempts=config.MAX_ATTEMPTS,
    )


@app.post("/webhooks/{source}")
async def receive(source: str, request: Request,
                  x_signature: str = Header(default="")):
    raw = await request.body()

    try:
        security.verify(raw, x_signature)
    except security.SignatureError as exc:
        # 401, not 400: the payload may be fine, the caller isn't trusted.
        raise HTTPException(status_code=401, detail=str(exc))

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="body is not valid JSON")

    event_id = payload.get("id")
    event_type = payload.get("type")
    if not event_id or not event_type:
        raise HTTPException(status_code=400, detail="payload needs 'id' and 'type'")

    routes = config.ROUTES.get(event_type)
    if routes is None:
        # Unrouted types are acknowledged, not errors: providers send plenty
        # of event types you never subscribed to, and a 4xx makes them retry.
        return JSONResponse({"status": "ignored", "type": event_type})

    with store.connect() as conn:
        created = store.record_event(
            conn, event_id, source, event_type, payload, routes
        )

    return JSONResponse({
        "status": "accepted" if created else "duplicate",
        "event_id": event_id,
        "queued": routes if created else [],
    })


@app.post("/admin/drain")
def drain():
    """Process the queue now instead of waiting for the scheduled worker."""
    delivered, retried, dead = worker.process_due()
    return {"delivered": delivered, "retried": retried, "dead": dead}


@app.post("/admin/requeue")
def requeue(event_id: str | None = None):
    """Put dead deliveries back in the queue after fixing whatever broke."""
    with store.connect() as conn:
        count = store.requeue_dead(conn, event_id)
    return {"requeued": count}


@app.get("/events")
def events():
    with store.connect() as conn:
        rows = store.recent_events(conn)
    return [{
        "event_id": row["event_id"],
        "source": row["source"],
        "type": row["event_type"],
        "received_at": row["received_at"],
    } for row in rows]


@app.get("/healthz")
def healthz():
    with store.connect() as conn:
        counts = store.counts(conn)
    # A non-empty 'dead' bucket is the thing worth paging on.
    return {
        "status": "degraded" if counts.get("dead") else "ok",
        "deliveries": counts,
        "dry_run": config.DRY_RUN,
    }
