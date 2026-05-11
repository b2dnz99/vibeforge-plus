"""
SSE event stream for real-time board updates.
GET /api/v2/projects/{slug}/events  — streams task.updated / task.created / agent.typing
"""
import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

router = APIRouter()

# In-process broadcast registry: slug -> list of subscriber queues
_subscribers: dict[str, list[asyncio.Queue]] = {}


async def _event_generator(request: Request, slug: str):
    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    _subscribers.setdefault(slug, []).append(q)
    try:
        # Send a heartbeat immediately so the browser knows the connection is alive
        yield {"event": "ping", "data": json.dumps({"ts": datetime.now(timezone.utc).isoformat()})}
        while True:
            if await request.is_disconnected():
                break
            try:
                # Short timeout = faster disconnect detection when client navigates away
                event = await asyncio.wait_for(q.get(), timeout=5)
                yield event
            except asyncio.TimeoutError:
                # Check disconnect before sending keepalive
                if await request.is_disconnected():
                    break
                yield {"event": "ping", "data": json.dumps({"ts": datetime.now(timezone.utc).isoformat()})}
    finally:
        subs = _subscribers.get(slug, [])
        if q in subs:
            subs.remove(q)
        if not subs and slug in _subscribers:
            del _subscribers[slug]


@router.get("/api/v2/projects/{slug}/events")
async def project_events(slug: str, request: Request):
    return EventSourceResponse(_event_generator(request, slug))


# --- Public helper for other routers to broadcast events ---

def broadcast(slug: str, event_name: str, data: dict):
    """Fire-and-forget broadcast to all SSE subscribers for a project."""
    payload = json.dumps(data)
    for q in _subscribers.get(slug, []):
        try:
            q.put_nowait({"event": event_name, "data": payload})
        except asyncio.QueueFull:
            pass  # slow subscriber — drop rather than block
