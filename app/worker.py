"""Drain the delivery queue.

Run it on a short interval (or as a loop under systemd). Deliveries that fail
are rescheduled with exponential backoff; after MAX_ATTEMPTS they are parked
as 'dead' and reported, never silently dropped.
"""

import json
import logging

from app import config, destinations, store

log = logging.getLogger(__name__)


def process_due(limit=50):
    """Attempt every delivery that is due. Returns (delivered, retried, dead)."""
    delivered = retried = dead = 0

    with store.connect() as conn:
        rows = store.due_deliveries(conn, limit=limit)

        for row in rows:
            payload = json.loads(row["payload"])
            try:
                destinations.deliver(
                    row["destination"], row["event_type"], payload
                )
            except destinations.DeliveryError as exc:
                outcome = store.mark_failed(
                    conn, row["id"], row["attempts"],
                    config.MAX_ATTEMPTS, str(exc),
                )
                if outcome == "dead":
                    dead += 1
                    log.error("delivery %s dead after %d attempts: %s",
                              row["id"], config.MAX_ATTEMPTS, exc)
                else:
                    retried += 1
                    log.warning("delivery %s failed, will retry: %s",
                                row["id"], exc)
                continue

            store.mark_delivered(conn, row["id"])
            delivered += 1

    return delivered, retried, dead


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    store.init()
    delivered, retried, dead = process_due()
    log.info("delivered=%d retried=%d dead=%d", delivered, retried, dead)
    return 1 if dead else 0


if __name__ == "__main__":
    raise SystemExit(main())
