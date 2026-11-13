"""Constants fixed by the assignment specification (not by the workflow schemas)."""
from __future__ import annotations

EVENT_INITIATED = "call.initiated"
EVENT_TELEPHONY_FAILED = "call.telephony_failed"
EVENT_CONNECTED = "call.connected"
EVENT_ENDED = "call.ended"
EVENT_DISPOSITION_SET = "disposition.set"
EVENT_DISPOSITION_OVERRIDE = "disposition.override"

EVENT_TYPES = (
    EVENT_INITIATED,
    EVENT_TELEPHONY_FAILED,
    EVENT_CONNECTED,
    EVENT_ENDED,
    EVENT_DISPOSITION_SET,
    EVENT_DISPOSITION_OVERRIDE,
)
DISPOSITION_EVENT_TYPES = (EVENT_DISPOSITION_SET, EVENT_DISPOSITION_OVERRIDE)

# Quarantine statuses (see models.QuarantinedEvent)
STATUS_ORPHAN = "orphan"
STATUS_UNKNOWN_DISPOSITION = "unknown_disposition"
STATUS_PROMOTED = "promoted"

# Lead buckets, in priority order (README "Derived facts")
BUCKET_YET_TO_REACH_OUT = "yet_to_reach_out"
BUCKET_COMPLETED = "completed"
BUCKET_TELEPHONY_FAILED = "in_progress_telephony_failed"
BUCKET_DIALED = "in_progress_dialed"
