"""Provider-neutral realtime session and WebSocket gateway."""

from app.modules.realtime.fake_provider import FakeRealtimeProvider
from app.modules.realtime.gateway import RealtimeGateway
from app.modules.realtime.router import build_realtime_router
from app.modules.realtime.sessions import (
    AuthenticatedUser,
    InMemoryRealtimeSessionStore,
    RealtimeSession,
)

__all__ = [
    "AuthenticatedUser",
    "FakeRealtimeProvider",
    "InMemoryRealtimeSessionStore",
    "RealtimeGateway",
    "RealtimeSession",
    "build_realtime_router",
]
