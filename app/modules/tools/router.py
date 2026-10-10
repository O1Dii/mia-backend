"""``ToolRouter`` implementation (port ``app.core.ports.tool_router``, Review §5.3-§5.4).

One entry point for every tool a run may call:

- plugin tools of the user's **active** installations, served by MCP servers
  (in-process builtin/declarative, or remote Streamable HTTP behind the SSRF
  guard); the manifest (``mia.json``) supplies risk/voice/timeout, the server
  supplies description and ``inputSchema``; a ``tools/list`` whose digest
  differs from the consented ``installation.tools_digest`` blocks the plugin;
- device tools declared by the run's device (``DeviceToolChannel``);
- core tools (``background.*``/``memory.*`` extension points, ``skills.load``).

Policy per call: lookup in the caller's own tool table (unknown/foreign →
``tool_not_found``), JSON-Schema arguments, profile/scopes, ``write_external`` →
approval bound to ``args_hash`` (single use, expiring), ``operation_id`` audited
before dispatch, one attempt only (timeout of a write → ``uncertain``). Tool
output reaches the model only framed as untrusted data. A screen-only action
(``_meta["mia/action"]``, M41-R1) of an in-process server is validated and handed
to the UI with the outcome; remote servers cannot attach one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from app.core.ports.approval_service import (
    ApprovalError,
    ApprovalRequest,
    ApprovalService,
    canonical_args_hash,
)
from app.core.ports.device_tool_channel import (
    DeviceToolCall,
    DeviceToolChannel,
    DeviceToolDeclaration,
)
from app.core.ports.model_gateway import ToolSpec
from app.core.ports.screen_action import action_from_meta
from app.core.ports.tool_router import (
    ToolCallRequest,
    ToolCallStatus,
    ToolContext,
    ToolDescriptor,
    ToolOutcome,
)
from app.core.ports.types import JsonObject, Risk
from app.modules.plugins.registry import InstalledPlugin, PluginRegistry
from app.modules.plugins.registry.installed import SKILL_DESCRIPTION_MAX
from app.modules.tools.approvals import ConsumableApprovals
from app.modules.tools.audit import AuditEntry, ToolAudit, content_digest
from app.modules.tools.core_tools import CoreTool, CoreToolRegistry
from app.modules.tools.digest import ServerTool, compute_tools_digest
from app.modules.tools.mcp_client import ConnectionRequired, McpConnector, McpFailure
from app.modules.tools.policy import (
    MAX_ARGUMENT_BYTES,
    approval_summary,
    arguments_valid,
    clip_description,
    compile_schema,
    encoded_arguments,
    structured_object,
    untrusted,
)

logger = logging.getLogger(__name__)

DEVICE_TOOL_NAME = re.compile(r"^(device|telegram)\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
SKILLS_TOOL = "skills.load"
_SKILL_TAG = re.compile(r"<\s*/?\s*skill", re.IGNORECASE)
EntryKind = Literal["plugin", "core", "device", "skills"]
_STATUS_CODES = {
    "needs_connection": "connection_required",
    "needs_consent": "tool_consent_required",
}


@dataclass(frozen=True, slots=True)
class RouterConfig:
    call_timeout_s: float = 20.0
    """Default ``tools/call`` deadline when the manifest sets no ``timeoutSeconds``."""
    list_cache_s: float = 60.0
    approval_ttl_s: int = 120
    max_result_chars: int = 8000
    max_structured_bytes: int = 64 * 1024


@dataclass(frozen=True, slots=True)
class _Entry:
    descriptor: ToolDescriptor
    kind: EntryKind
    validator: Draft202012Validator | None
    timeout_s: float
    plugin: InstalledPlugin | None = None
    local_name: str | None = None
    ui_resource_uri: str | None = None
    core: CoreTool | None = None
    device: DeviceToolDeclaration | None = None

    @property
    def risk(self) -> Risk:
        return self.descriptor.risk


@dataclass(slots=True)
class _Table:
    entries: dict[str, _Entry] = field(default_factory=dict)
    blocked: dict[str, str] = field(default_factory=dict)
    """Known but unusable tool name → error code (consent, status, connection)."""
    plugins: dict[str, InstalledPlugin] = field(default_factory=dict)
    """Usable plugins by id (``skills.load`` targets)."""

    def add(self, entry: _Entry) -> None:
        name = entry.descriptor.spec.name
        if name in self.entries:
            logger.warning("tool_name_collision", extra={"tool": name})
            return
        self.entries[name] = entry


def _hex_args(arguments: object) -> str:
    encoded = encoded_arguments(arguments)
    return hashlib.sha256(encoded or b"invalid-arguments").hexdigest()


def _ui_uri(meta: JsonObject | None) -> str | None:
    if not meta:
        return None
    ui = meta.get("ui")
    uri = ui.get("resourceUri") if isinstance(ui, dict) else None
    if isinstance(uri, str) and uri.startswith("ui://") and len(uri) <= 512:
        return uri
    return None


def _skill_frame(plugin_id: str, bodies: Sequence[str]) -> str:
    text = "\n\n".join(_SKILL_TAG.sub(lambda m: m.group(0).replace("<", "‹"), b) for b in bodies)
    return f'<skill plugin="{plugin_id}">\n{text}\n</skill>'


class McpToolRouter:
    def __init__(
        self,
        *,
        registry: PluginRegistry,
        connector: McpConnector,
        audit: ToolAudit,
        approvals: ApprovalService | None = None,
        device_channel: DeviceToolChannel | None = None,
        core_tools: CoreToolRegistry | None = None,
        config: RouterConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = registry
        self._connector = connector
        self._audit = audit
        self._approvals = approvals
        self._device_channel = device_channel
        self.core_tools = core_tools or CoreToolRegistry()
        self._config = config or RouterConfig()
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._tool_cache: dict[tuple[str, str], tuple[float, list[ServerTool]]] = {}
        self._consumed: set[str] = set()

    # -- discovery --------------------------------------------------------------

    async def list_tools(self, context: ToolContext) -> Sequence[ToolDescriptor]:
        table = await self._table(context)
        return [entry.descriptor for entry in table.entries.values()]

    async def _server_tools(self, plugin: InstalledPlugin, user_id: str) -> list[ServerTool]:
        if plugin.manifest.server.in_process:
            key = (plugin.manifest.plugin_id, plugin.manifest.version)
        else:
            key = (plugin.installation.id, plugin.manifest.version)
        cached = self._tool_cache.get(key)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        tools = await self._connector.list_tools(plugin, user_id=user_id)
        self._tool_cache[key] = (now + self._config.list_cache_s, tools)
        return tools

    def invalidate(self, plugin_id: str | None = None) -> None:
        """Drop cached ``tools/list`` results (after reinstall/consent)."""
        if plugin_id is None:
            self._tool_cache.clear()
            return
        self._tool_cache = {
            key: value for key, value in self._tool_cache.items() if key[0] != plugin_id
        }

    def _visible_to_app(self, context: ToolContext, plugin_id: str) -> bool:
        return "tools.use" in context.scopes or f"tools.use:{plugin_id}" in context.scopes

    async def _plugin_entries(
        self, context: ToolContext, plugin: InstalledPlugin, table: _Table
    ) -> None:
        manifest = plugin.manifest
        names = [f"{manifest.plugin_id}.{local}" for local in manifest.tools]
        status = plugin.installation.status

        def block(code: str) -> None:
            for name in names:
                table.blocked.setdefault(name, code)

        if context.profile == "app" and not self._visible_to_app(context, manifest.plugin_id):
            block("tool_permission_denied")
            return
        if status != "active":
            block(
                {
                    "needs_connection": "connection_required",
                    "needs_consent": "tool_consent_required",
                }.get(status, "tool_not_active")
            )
            return
        try:
            server_tools = await self._server_tools(plugin, context.user_id)
        except ConnectionRequired:
            block("connection_required")
            return
        except McpFailure as failure:
            logger.warning(
                "plugin_tools_unavailable",
                extra={"plugin_id": manifest.plugin_id, "kind": failure.kind},
            )
            block("tool_host_blocked" if failure.kind == "blocked" else "tool_unavailable")
            return
        digest = compute_tools_digest(server_tools)
        if plugin.installation.tools_digest is None or digest != plugin.installation.tools_digest:
            logger.warning("plugin_tools_digest_mismatch", extra={"plugin_id": manifest.plugin_id})
            block("tool_consent_required")
            return
        by_name = {tool.name: tool for tool in server_tools}
        usable = False
        for local, policy in manifest.tools.items():
            name = f"{manifest.plugin_id}.{local}"
            server_tool = by_name.get(local)
            validator = compile_schema(server_tool.input_schema) if server_tool else None
            if server_tool is None or validator is None:
                continue
            if context.profile == "voice" and not policy.voice:
                table.blocked.setdefault(name, "tool_permission_denied")
                continue
            description = clip_description(f"{manifest.name}: {server_tool.description}")
            table.add(
                _Entry(
                    descriptor=ToolDescriptor(
                        spec=ToolSpec(
                            name=name,
                            description=description,
                            input_schema=dict(server_tool.input_schema),
                        ),
                        risk=policy.risk,
                        executor="server",
                        voice=policy.voice,
                        plugin_id=manifest.plugin_id,
                    ),
                    kind="plugin",
                    validator=validator,
                    timeout_s=float(policy.timeout_seconds or self._config.call_timeout_s),
                    plugin=plugin,
                    local_name=local,
                    ui_resource_uri=_ui_uri(server_tool.meta),
                )
            )
            usable = True
        if usable:
            table.plugins[manifest.plugin_id] = plugin

    def _core_entries(self, context: ToolContext, table: _Table) -> None:
        for tool in self.core_tools.tools():
            if context.profile not in tool.profiles:
                continue
            if context.profile == "voice" and not tool.voice:
                continue
            validator = compile_schema(tool.input_schema)
            if validator is None:
                logger.warning("core_tool_schema_invalid", extra={"tool": tool.name})
                continue
            table.add(
                _Entry(
                    descriptor=ToolDescriptor(
                        spec=ToolSpec(tool.name, tool.description, dict(tool.input_schema)),
                        risk=tool.risk,
                        executor="server",
                        voice=tool.voice,
                    ),
                    kind="core",
                    validator=validator,
                    timeout_s=tool.timeout_s,
                    core=tool,
                )
            )

    async def _device_entries(self, context: ToolContext, table: _Table) -> None:
        channel = self._device_channel
        if channel is None or context.device_id is None or context.profile not in ("voice", "chat"):
            return
        declared = await channel.available_tools(
            user_id=context.user_id, device_id=context.device_id
        )
        for tool in declared:
            if not DEVICE_TOOL_NAME.match(tool.name):
                continue
            if context.profile == "voice" and not tool.voice:
                continue
            validator = compile_schema(tool.input_schema)
            if validator is None:
                continue
            table.add(
                _Entry(
                    descriptor=ToolDescriptor(
                        spec=ToolSpec(
                            tool.name, clip_description(tool.description), dict(tool.input_schema)
                        ),
                        risk=tool.risk,
                        executor="device",
                        voice=tool.voice,
                    ),
                    kind="device",
                    validator=validator,
                    timeout_s=tool.timeout_ms / 1000,
                    device=tool,
                )
            )

    async def _skills_entry(self, context: ToolContext, table: _Table) -> None:
        if context.profile in ("voice", "app") or not table.plugins:
            return
        lines: list[str] = []
        targets: list[str] = []
        for plugin_id, plugin in table.plugins.items():
            for summary in await self._registry.skill_summaries(plugin):
                lines.append(f"- {plugin_id}: {summary.description[:SKILL_DESCRIPTION_MAX]}")
                if plugin_id not in targets:
                    targets.append(plugin_id)
        if not targets:
            return
        description = clip_description(
            "Load the full instructions (SKILL.md) of an installed plugin before using it. "
            "Skills: " + " ".join(lines)
        )
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"plugin_id": {"type": "string", "enum": targets}},
            "required": ["plugin_id"],
            "additionalProperties": False,
        }
        table.add(
            _Entry(
                descriptor=ToolDescriptor(
                    spec=ToolSpec(SKILLS_TOOL, description, schema),
                    risk="read",
                    executor="server",
                    voice=False,
                ),
                kind="skills",
                validator=compile_schema(schema),
                timeout_s=5.0,
            )
        )

    async def _table(self, context: ToolContext) -> _Table:
        table = _Table()
        self._core_entries(context, table)
        await self._device_entries(context, table)
        plugins = await self._registry.installed(
            user_id=context.user_id, workspace_id=context.workspace_id
        )
        # Fetch tool lists concurrently, then add entries in installation order.
        partial = [_Table() for _ in plugins]
        await asyncio.gather(
            *(
                self._plugin_entries(context, plugin, part)
                for plugin, part in zip(plugins, partial, strict=True)
            )
        )
        for part in partial:
            for entry in part.entries.values():
                table.add(entry)
            for name, code in part.blocked.items():
                table.blocked.setdefault(name, code)
            table.plugins.update(part.plugins)
        await self._skills_entry(context, table)
        return table

    # -- calls ------------------------------------------------------------------

    async def call(self, context: ToolContext, request: ToolCallRequest) -> ToolOutcome:
        table = await self._table(context)
        entry = table.entries.get(request.tool)
        plugin = entry.plugin if entry else None
        audit = AuditEntry(
            operation_id=uuid.uuid4(),
            user_id=context.user_id,
            run_id=context.run_id,
            tool_call_id=request.call_id,
            tool=request.tool,
            arguments_hash=_hex_args(request.arguments),
            risk=entry.risk if entry else None,
            executor=entry.descriptor.executor if entry else None,
            profile=context.profile,
            workspace_id=(
                entry.plugin.installation.workspace_id if entry and entry.plugin else None
            )
            or context.workspace_id,
            plugin_id=entry.descriptor.plugin_id if entry else None,
            installation_id=plugin.installation.id if plugin else None,
            approval_id=request.approval_id,
        )
        if entry is None:
            code = table.blocked.get(request.tool, "tool_not_found")
            return await self._refuse(audit, request, "failed", code)
        encoded = encoded_arguments(request.arguments)
        if (
            encoded is None
            or len(encoded) > MAX_ARGUMENT_BYTES
            or entry.validator is None
            or not arguments_valid(entry.validator, request.arguments)
        ):
            return await self._refuse(audit, request, "failed", "tool_invalid_arguments")
        approval_id: str | None = None
        if entry.risk == "write_external":
            if request.approval_id is None:
                return await self._request_approval(context, request, entry, audit)
            refused = await self._consume_approval(context, request, audit)
            if refused is not None:
                return refused
            approval_id = request.approval_id
        return await self._dispatch(context, request, entry, audit, approval_id, table)

    async def _refuse(
        self, audit: AuditEntry, request: ToolCallRequest, status: ToolCallStatus, code: str
    ) -> ToolOutcome:
        await self._safe_record(audit, status, code)
        return ToolOutcome(call_id=request.call_id, status=status, error_code=code)

    async def _safe_record(self, audit: AuditEntry, status: str, code: str | None) -> None:
        try:
            await self._audit.record(audit, status=status, error_code=code)
        except Exception:
            logger.exception("tool_audit_failed", extra={"tool": audit.tool})

    async def _request_approval(
        self, context: ToolContext, request: ToolCallRequest, entry: _Entry, audit: AuditEntry
    ) -> ToolOutcome:
        if self._approvals is None:
            return await self._refuse(audit, request, "denied", "approval_unavailable")
        plugin = entry.plugin
        summary = approval_summary(
            plugin.manifest.name if plugin else request.tool.split(".", 1)[0],
            entry.local_name or request.tool,
            request.arguments,
        )
        approval = await self._approvals.request(
            ApprovalRequest(
                user_id=context.user_id,
                run_id=context.run_id,
                tool_call_id=request.call_id,
                tool=request.tool,
                risk=entry.risk,
                summary=summary,
                arguments=dict(request.arguments),
                plugin_id=entry.descriptor.plugin_id,
                ttl_seconds=self._config.approval_ttl_s,
            )
        )
        await self._safe_record(
            dataclasses.replace(audit, approval_id=approval.id), "approval_required", None
        )
        return ToolOutcome(call_id=request.call_id, status="approval_required", approval=approval)

    async def _consume_approval(
        self, context: ToolContext, request: ToolCallRequest, audit: AuditEntry
    ) -> ToolOutcome | None:
        approvals = self._approvals
        approval_id = request.approval_id
        assert approval_id is not None
        if approvals is None:
            return await self._refuse(audit, request, "denied", "approval_unavailable")
        args_hash = canonical_args_hash(request.arguments)
        try:
            if isinstance(approvals, ConsumableApprovals):
                await approvals.consume(
                    user_id=context.user_id,
                    approval_id=approval_id,
                    run_id=context.run_id,
                    tool=request.tool,
                    args_hash=args_hash,
                )
            else:
                await self._consume_generic(approvals, context, request, args_hash)
        except ApprovalError as error:
            status: ToolCallStatus = "expired" if error.code == "approval_expired" else "denied"
            return await self._refuse(audit, request, status, error.code)
        return None

    async def _consume_generic(
        self,
        approvals: ApprovalService,
        context: ToolContext,
        request: ToolCallRequest,
        args_hash: str,
    ) -> None:
        """Single use for an ``ApprovalService`` without ``consume`` (process-local)."""
        approval_id = request.approval_id
        assert approval_id is not None
        approval = await approvals.get(user_id=context.user_id, approval_id=approval_id)
        if approval is None:
            raise ApprovalError("approval_not_found", "Approval not found")
        if approval.run_id != context.run_id or approval.tool != request.tool:
            raise ApprovalError("approval_mismatch", "Approval belongs to another action")
        if approval.args_hash != args_hash:
            raise ApprovalError("approval_args_mismatch", "Arguments changed")
        if approval.status != "approved":
            raise ApprovalError(f"approval_{approval.status}", "Approval is not usable")
        if self._clock() > approval.expires_at + timedelta(seconds=30):
            raise ApprovalError("approval_expired", "Approval expired")
        if approval_id in self._consumed:
            raise ApprovalError("approval_already_used", "Approval was already used")
        self._consumed.add(approval_id)

    async def _dispatch(
        self,
        context: ToolContext,
        request: ToolCallRequest,
        entry: _Entry,
        audit: AuditEntry,
        approval_id: str | None,
        table: _Table,
    ) -> ToolOutcome:
        try:
            await self._audit.begin(audit)
        except Exception:
            logger.exception("tool_audit_failed", extra={"tool": audit.tool})
            return ToolOutcome(
                call_id=request.call_id, status="failed", error_code="tool_execution_failed"
            )
        started = time.monotonic()
        try:
            if entry.kind == "plugin":
                outcome = await self._call_plugin(context, request, entry, audit)
            elif entry.kind == "core":
                outcome = await self._call_core(context, request, entry)
            elif entry.kind == "device":
                outcome = await self._call_device(context, request, entry, approval_id)
            else:
                outcome = await self._call_skills(request, table)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish(audit, "cancelled", "tool_execution_failed", None, started)
            )
            raise
        await self._finish(
            audit,
            outcome.status,
            outcome.error_code,
            content_digest(outcome.content) if outcome.content else None,
            started,
        )
        return outcome

    async def _finish(
        self,
        audit: AuditEntry,
        status: str,
        code: str | None,
        digest: str | None,
        started: float,
    ) -> None:
        try:
            await self._audit.finish(
                audit,
                status=status,
                error_code=code,
                result_digest=digest,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:
            logger.exception("tool_audit_failed", extra={"tool": audit.tool})

    def _identity_meta(
        self, context: ToolContext, plugin: InstalledPlugin, audit: AuditEntry
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "mia/installation_id": plugin.installation.id,
            "mia/operation_id": str(audit.operation_id),
        }
        if plugin.manifest.server.in_process:
            meta.update(
                {
                    "mia/user_id": context.user_id,
                    "mia/workspace_id": plugin.installation.workspace_id,
                    "mia/run_id": context.run_id,
                }
            )
        return meta

    def _success(
        self,
        request: ToolCallRequest,
        *,
        text: str,
        structured: object | None,
        ui_resource_uri: str | None,
        status: ToolCallStatus = "succeeded",
        error_code: str | None = None,
        action: JsonObject | None = None,
    ) -> ToolOutcome:
        return ToolOutcome(
            call_id=request.call_id,
            status=status,
            content=untrusted(request.tool, text, limit=self._config.max_result_chars),
            structured_content=structured_object(
                structured, limit_bytes=self._config.max_structured_bytes
            ),
            ui_resource_uri=ui_resource_uri if status == "succeeded" else None,
            error_code=error_code,
            action=action if status == "succeeded" else None,
        )

    async def _call_plugin(
        self, context: ToolContext, request: ToolCallRequest, entry: _Entry, audit: AuditEntry
    ) -> ToolOutcome:
        plugin, local = entry.plugin, entry.local_name
        assert plugin is not None and local is not None
        try:
            result = await self._connector.call_tool(
                plugin,
                user_id=context.user_id,
                name=local,
                arguments=request.arguments,
                meta=self._identity_meta(context, plugin, audit),
                timeout_s=entry.timeout_s,
            )
        except ConnectionRequired:
            return ToolOutcome(request.call_id, "failed", error_code="connection_required")
        except McpFailure as failure:
            return self._failure_outcome(request, entry.risk, failure)
        if result.is_error:
            return self._success(
                request,
                text=result.text,
                structured=None,
                ui_resource_uri=None,
                status="failed",
                error_code="tool_execution_failed",
            )
        # Only trusted in-process servers may put a button on the user's screen.
        trusted = plugin.manifest.server.in_process
        return self._success(
            request,
            text=result.text,
            structured=result.structured,
            ui_resource_uri=entry.ui_resource_uri,
            action=action_from_meta(result.meta) if trusted else None,
        )

    @staticmethod
    def _failure_outcome(request: ToolCallRequest, risk: Risk, failure: McpFailure) -> ToolOutcome:
        if failure.kind == "blocked":
            return ToolOutcome(request.call_id, "failed", error_code="tool_host_blocked")
        code = {
            "timeout": "tool_timeout",
            "error_response": "tool_execution_failed",
        }.get(failure.kind, "tool_unavailable")
        delivered = failure.phase == "call" and failure.kind != "error_response"
        if delivered and risk != "read":
            # The write may have happened: report it, never retry it.
            return ToolOutcome(request.call_id, "uncertain", error_code=code)
        return ToolOutcome(request.call_id, "failed", error_code=code)

    async def _call_core(
        self, context: ToolContext, request: ToolCallRequest, entry: _Entry
    ) -> ToolOutcome:
        tool = entry.core
        assert tool is not None
        try:
            async with asyncio.timeout(entry.timeout_s):
                result = await tool.handler(context, dict(request.arguments))
        except TimeoutError:
            status: ToolCallStatus = "failed" if tool.risk == "read" else "uncertain"
            return ToolOutcome(request.call_id, status, error_code="tool_timeout")
        except Exception:
            logger.exception("core_tool_failed", extra={"tool": tool.name})
            status = "failed" if tool.risk == "read" else "uncertain"
            return ToolOutcome(request.call_id, status, error_code="tool_execution_failed")
        if result.status == "failed":
            return self._success(
                request,
                text=result.content,
                structured=None,
                ui_resource_uri=None,
                status="failed",
                error_code=result.error_code or "tool_execution_failed",
            )
        return self._success(
            request,
            text=result.content,
            structured=result.structured_content,
            ui_resource_uri=result.ui_resource_uri,
        )

    async def _call_device(
        self,
        context: ToolContext,
        request: ToolCallRequest,
        entry: _Entry,
        approval_id: str | None,
    ) -> ToolOutcome:
        channel = self._device_channel
        if channel is None or context.device_id is None:
            return ToolOutcome(request.call_id, "failed", error_code="device_unavailable")
        result = await channel.invoke(
            user_id=context.user_id,
            call=DeviceToolCall(
                call_id=request.call_id,
                run_id=context.run_id,
                device_id=context.device_id,
                tool=request.tool,
                arguments=dict(request.arguments),
                deadline_at=self._clock() + timedelta(seconds=entry.timeout_s),
                approval_id=approval_id,
            ),
        )
        if result.status == "succeeded":
            return self._success(
                request,
                text=result.content,
                structured=result.structured_content,
                ui_resource_uri=None,
            )
        code = f"device_{result.error_code}" if result.error_code else "device_execution_failed"
        if code == "device_device_locked":
            code = "device_locked"
        return ToolOutcome(request.call_id, result.status, error_code=code)

    async def _call_skills(self, request: ToolCallRequest, table: _Table) -> ToolOutcome:
        plugin_id = request.arguments.get("plugin_id")
        plugin = table.plugins.get(plugin_id) if isinstance(plugin_id, str) else None
        if plugin is None:
            return ToolOutcome(request.call_id, "failed", error_code="tool_invalid_arguments")
        bodies = await self._registry.skill_bodies(plugin)
        if not bodies:
            return ToolOutcome(request.call_id, "failed", error_code="tool_not_found")
        return ToolOutcome(
            call_id=request.call_id,
            status="succeeded",
            content=_skill_frame(plugin.plugin_id, bodies)[: self._config.max_result_chars * 2],
        )
