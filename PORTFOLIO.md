# Upwork portfolio item — copy/paste

**Project title**

```
Webhook Integration Service — idempotent delivery with retries
```

**Description** (~590 characters)

```
A service that receives webhooks from one system and reliably pushes them into others — Slack and Google Sheets in this build.

Every webhook provider retries, so your endpoint will receive the same event twice. Without protection that means duplicate messages, duplicate spreadsheet rows, duplicate side effects. This handles it: HMAC signature verification with replay protection, a unique event key so repeats are a no-op, exponential-backoff retries, and a requeue button for after you've fixed an outage.

It ships with an operations page showing what arrived, what was delivered, what is queued and what gave up — so you find out before your client does.

Python, FastAPI, SQLite. 13 passing tests covering dedupe, bad signatures and the retry-to-dead path.
```

**Skills to tag**

Python, API Integration, API Development, FastAPI, Automation, REST API, SQL

**Image to upload**

`screenshots/operations.jpg` — the operations page after the sample run:
3 events stored, 3 repeats ignored, 5 delivered, 0 needing attention.

**Project URL**

Your GitHub repo link, once pushed.

---

## Talking points if a client asks about it on a call

- *"Why does duplication matter?"* — Point at "Repeats ignored" on the
  operations page. Eight requests in, five deliveries out. That is the entire
  product, and the page counts it for you.
- *"What if Slack is down?"* — The delivery retries with backoff, five times by
  default. After that it's parked as `dead` and `/healthz` reports `degraded`
  instead of silently dropping it. You fix Slack, hit `/admin/requeue`, it goes.
- *"Can you connect X instead?"* — A destination is one function. Adding one is
  an hour, not a rewrite.
- *"How do I know it's working?"* — `/healthz` gives delivery counts and flags
  anything dead. Point your uptime monitor at it.
- *"Is it secure?"* — HMAC-SHA256, constant-time comparison, and a timestamp
  tolerance so an old captured request can't be replayed.
