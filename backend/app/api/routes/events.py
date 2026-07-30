"""Event history and the live project WebSocket.

The `events` table is the durable record; the WebSocket is a live view of it.
Every event is persisted before it is broadcast, so a client that misses frames
loses nothing it cannot recover by replaying from a sequence number.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.api.deps import DbSession, ProjectRead
from app.api.schemas.events import EventEnvelope
from app.core.config import Settings, get_settings
from app.core.enums import EventType, ProjectRole
from app.core.errors import AppError, AuthenticationError
from app.core.logging import get_logger
from app.database.session import get_session_factory
from app.models.user import User
from app.orchestration.broadcast import get_broker
from app.orchestration.events import envelope_of, read_events
from app.security import permissions
from app.security.tokens import decode_access_token

logger = get_logger(__name__)

#: REST history, mounted under the /api prefix.
router = APIRouter(tags=["events"])

#: The WebSocket, mounted at the root because /ws is not part of the REST API.
#: A separate router so that including it does not also re-expose the REST route
#: at the root path.
ws_router = APIRouter(tags=["events"])

#: Sent when the client has been silent, so a dead connection is noticed rather
#: than held open forever by a NAT that never sends a FIN.
HEARTBEAT_SECONDS = 25.0


@router.get(
    "/projects/{project_id}/events",
    response_model=list[EventEnvelope],
    summary="Replay a project's event history",
)
def list_events(
    access: ProjectRead,
    db: DbSession,
    since_seq: Annotated[int | None, Query(ge=0)] = None,
    event_type: Annotated[list[EventType] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EventEnvelope]:
    """Events in sequence order.

    Pass the last `seq` you saw as `since_seq` to fetch only what followed. This
    is the same ordering the WebSocket delivers, so the two are interchangeable
    for catching up.
    """
    rows, _ = read_events(
        db,
        access.project.id,
        since_seq=since_seq,
        event_types=event_type,
        limit=limit,
        offset=offset,
    )
    return [EventEnvelope.model_validate(envelope_of(row)) for row in rows]


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
def _authenticate_socket(
    db: Session, token: str, settings: Settings, project_id: uuid.UUID
) -> User:
    """Authenticate and authorise a WebSocket connection.

    A browser cannot set headers on a WebSocket handshake, so the token arrives
    as a query parameter or a cookie. That is why the same authorisation the REST
    routes apply is repeated here rather than assumed: an upgrade request is not
    automatically trusted just because it reached this endpoint.
    """
    from app.core.services import users as user_service

    claims = decode_access_token(token, settings)
    if user_service.is_token_revoked(db, claims.jti):
        raise AuthenticationError("Session has been signed out.")

    user = db.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is not active.")

    role = permissions.project_role_for(db, user, project_id)
    permissions.require_project_access(role, frozenset(ProjectRole), operation="watch this project")
    return user


@ws_router.websocket("/ws/projects/{project_id}")
async def project_events(
    websocket: WebSocket,
    project_id: uuid.UUID,
    token: Annotated[str | None, Query()] = None,
    since_seq: Annotated[int | None, Query(ge=0)] = None,
) -> None:
    """Stream a project's events.

    Connect with `?token=<access token>` (or rely on the session cookie) and
    optionally `?since_seq=<n>` to replay what was missed.

    The server answers with a `hello` frame carrying `current_seq`, how many
    events were replayed, and `replay_truncated`. When that flag is true the
    requested point is older than the replay window and the client must refetch
    state over REST rather than assume it has a complete picture — quietly
    delivering a partial history would be worse than saying so.
    """
    # From the application, not the process-wide singleton. A host that builds
    # the app with explicit Settings — which ``create_app`` invites, and which
    # every test does — would otherwise have the socket authenticate against a
    # different JWT secret and cookie name than the REST API beside it, and
    # reject a token the API had just issued.
    settings: Settings = getattr(websocket.app.state, "settings", None) or get_settings()
    presented = token or websocket.cookies.get(settings.session_cookie_name, "")

    session_factory = get_session_factory()
    db = session_factory()
    try:
        if not presented:
            await websocket.close(code=4401, reason="Authentication required.")
            return
        try:
            _authenticate_socket(db, presented, settings, project_id)
        except AppError as exc:
            # 4401/4403 are application close codes; the handshake itself
            # succeeded, so the reason has to travel in the close frame.
            await websocket.close(code=4401 if exc.status_code == 401 else 4403, reason=exc.message)
            return

        await websocket.accept()

        rows, current_seq = read_events(
            db,
            project_id,
            since_seq=since_seq,
            limit=settings.ws_replay_limit,
        )
        replayed = [envelope_of(row) for row in rows]
        truncated = (
            since_seq is not None
            and len(rows) == settings.ws_replay_limit
            and current_seq > (rows[-1].seq if rows else 0)
        )

        await websocket.send_json(
            {
                "type": "hello",
                "project_id": str(project_id),
                "schema_version": settings.event_schema_version,
                "current_seq": current_seq,
                "replay_truncated": truncated,
                "replayed": len(replayed),
            }
        )
        for past in replayed:
            await websocket.send_json(past)

        last_sent = replayed[-1]["seq"] if replayed else (since_seq or 0)
    finally:
        db.close()

    broker = get_broker()
    async with broker.subscribe(project_id) as queue:
        try:
            while True:
                try:
                    envelope: dict[str, Any] = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_SECONDS
                    )
                except TimeoutError:
                    await websocket.send_json({"type": "ping"})
                    continue

                # An event recorded between the replay read and the subscription
                # would otherwise be delivered twice.
                if int(envelope.get("seq", 0)) <= last_sent:
                    continue
                last_sent = int(envelope.get("seq", 0))
                await websocket.send_json(envelope)
        except WebSocketDisconnect:
            logger.info("websocket client disconnected", extra={"project_id": str(project_id)})
        except Exception:
            logger.exception("websocket stream error", extra={"project_id": str(project_id)})
            with contextlib.suppress(Exception):
                await websocket.close(code=1011, reason="Internal error.")
