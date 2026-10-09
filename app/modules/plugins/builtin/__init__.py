"""Host of in-process (builtin and declarative) plugin MCP servers."""

from app.modules.plugins.builtin.factory import (
    InprocessFactory,
    UnsupportedServerError,
    build_inprocess_server,
    make_inprocess_factory,
)
from app.modules.plugins.builtin.identity import CallIdentity, identity_from_meta
from app.modules.plugins.builtin.manifests import (
    BUILTIN_ROOT,
    BuiltinManifestSource,
    BuiltinPlugin,
)
from app.modules.plugins.builtin.services import HostLlm, HostLlmError, HostServices, LlmReply
from app.modules.plugins.builtin.state import (
    InMemoryPluginStateStore,
    PluginStateStore,
    SqlPluginStateStore,
)

__all__ = [
    "BUILTIN_ROOT",
    "BuiltinManifestSource",
    "BuiltinPlugin",
    "CallIdentity",
    "HostLlm",
    "HostLlmError",
    "HostServices",
    "InMemoryPluginStateStore",
    "InprocessFactory",
    "LlmReply",
    "PluginStateStore",
    "SqlPluginStateStore",
    "UnsupportedServerError",
    "build_inprocess_server",
    "identity_from_meta",
    "make_inprocess_factory",
]
