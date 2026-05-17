"""In-process event bus for SSE streaming.

Each analysis_id has an append-only log of events plus a 'done' flag. Subscribers
poll the log and yield new events. This is race-free, lossless, and lets late
subscribers replay everything from event #0.

Single-process only. If we ever scale to multiple workers, swap the backing
store for Redis Streams or Postgres LISTEN/NOTIFY.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, AsyncIterator

_log: dict[str, list[dict[str, Any]]] = defaultdict(list)
_done: set[str] = set()


def publish(analysis_id: str, event_type: str, **payload: Any) -> None:
    """Append a new event to the log."""
    seq = len(_log[analysis_id])
    msg: dict[str, Any] = {"seq": seq, "type": event_type, **payload}
    _log[analysis_id].append(msg)
    if event_type == "done" or event_type == "error":
        _done.add(analysis_id)


async def subscribe(analysis_id: str, since: int = 0) -> AsyncIterator[dict[str, Any]]:
    """Yield all events for `analysis_id` from `since` onwards, then live-stream.

    Returns when a `done` or `error` event has been replayed.
    """
    while True:
        log = _log.get(analysis_id, [])
        if len(log) > since:
            for msg in log[since:]:
                yield msg
                since = msg["seq"] + 1
        if analysis_id in _done and len(_log.get(analysis_id, [])) <= since:
            return
        await asyncio.sleep(0.2)


def get_log(analysis_id: str) -> list[dict[str, Any]]:
    """Synchronous snapshot of all events. For GET /analyze/{id} state endpoint."""
    return list(_log.get(analysis_id, []))


def clear(analysis_id: str) -> None:
    """Free memory after the run is wrapped."""
    _log.pop(analysis_id, None)
    _done.discard(analysis_id)
