import time
import uuid
from typing import Any, Dict, Optional, List

_QUEUE: List[Dict[str, Any]] = []

def enqueue(alert: dict, channel_id: str, thread_ts: str) -> dict:
    item = {
        "id": str(uuid.uuid4()),
        "created_at": int(time.time()),
        "status": "pending",
        "alert": alert,
        "channel_id": channel_id,
        "thread_ts": thread_ts,
    }
    _QUEUE.append(item)
    return item

def take_next() -> Optional[dict]:
    for item in _QUEUE:
        if item.get("status") == "pending":
            item["status"] = "taken"
            return item
    return None
