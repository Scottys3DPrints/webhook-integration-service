"""Settings, all overridable by environment variable."""

import os

DB_PATH = os.environ.get("WEBHOOK_SYNC_DB", "webhook_sync.db")

# Shared secret the sender signs with. Never commit a real one.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "dev-secret-change-me")

# Reject signatures older than this, so a captured request can't be replayed
# days later. Stripe uses 300s; so do we.
SIGNATURE_TOLERANCE_SECONDS = int(os.environ.get("WEBHOOK_TOLERANCE", "300"))

# Delivery retry policy. Attempt N waits BASE_BACKOFF * 2**(N-1) seconds.
MAX_ATTEMPTS = int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "5"))
BASE_BACKOFF_SECONDS = int(os.environ.get("WEBHOOK_BASE_BACKOFF", "30"))

# With DRY_RUN on, destinations log what they would send instead of sending.
# This is how the project runs with no credentials at all.
DRY_RUN = os.environ.get("WEBHOOK_DRY_RUN", "1") not in ("0", "false", "False")

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# A Google Apps Script web app or Sheets-proxy endpoint that accepts a row as
# JSON. Kept generic on purpose: clients have wildly different sheet setups.
SHEETS_WEBHOOK_URL = os.environ.get("SHEETS_WEBHOOK_URL", "")

HTTP_TIMEOUT = 10

# Which destinations each event type fans out to.
ROUTES = {
    "payment.succeeded": ["slack", "sheets"],
    "payment.failed": ["slack"],
    "subscription.cancelled": ["slack", "sheets"],
}
