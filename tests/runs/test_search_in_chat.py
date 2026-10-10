"""M73 DoD: a news question is answered in the chat with links, without a background task.

The real tool router serves the builtin «Интернет» plugin to a user who installed nothing;
the web and the model are recorded fakes. The tool-call journal (``tool.call.*``) carries
the arguments and the reason of a failure.
"""

from __future__ import annotations

import json
from typing import Any, cast

from app.core.ports.tool_router import ToolContext
from app.core.ports.types import JsonObject
from app.modules.jobs.tools import JobTools, job_core_tools
from app.modules.plugins.builtin import BuiltinManifestSource, make_inprocess_factory
from app.modules.plugins.composition import (
    AlwaysOnInstallations,
    BuiltinPluginCatalog,
    RouterServerFactory,
)
from app.modules.plugins.registry import (
    DirectoryManifestSource,
    PluginRegistry,
    StaticInstallationReader,
)
from app.modules.runs import NewRun, RunService
from app.modules.tools.approvals import InMemoryApprovalService
from app.modules.tools.audit import InMemoryToolAudit
from app.modules.tools.composition import BUILTIN_PLUGINS_DIR
from app.modules.tools.core_tools import CoreTool, CoreToolRegistry, CoreToolResult
from app.modules.tools.mcp_client import McpConnector
from app.modules.tools.router import McpToolRouter, RouterConfig

from tests.fakes import FakeModelGateway, text_step, tool_step
from tests.plugins.support import FakeWeb, make_services
from tests.runs.conftest import RunWorld, collect, payload_of, types
from tests.runs.gateways import answer

NEWS: list[dict[str, Any]] = [
    {
        "title": "Первая новость",
        "url": "https://example.com/a",
        "source": "Example",
        "published_at": "Sat, 10 Oct 2026 07:00:00 GMT",
        "snippet": "Описание",
    },
    {
        "title": "Вторая новость",
        "url": "https://example.org/b",
        "source": "Org",
        "published_at": "Sat, 10 Oct 2026 08:00:00 GMT",
        "snippet": "",
    },
]
QUESTION = "Подготовь краткую сводку главных новостей за сегодня"


class _JobStub:
    """Stands in for ``JobTools``: every handler records that it was called."""

    def __init__(self) -> None:
        self.started: list[JsonObject] = []

    def __getattr__(self, name: str) -> Any:
        async def handler(context: ToolContext, args: JsonObject) -> CoreToolResult:
            self.started.append(args)
            return CoreToolResult(content="started")

        return handler


def build_router(web: FakeWeb) -> tuple[McpToolRouter, _JobStub, CoreToolRegistry]:
    source = BuiltinManifestSource()
    server_factory = RouterServerFactory(make_inprocess_factory(make_services(web=web)))
    catalog = BuiltinPluginCatalog(source, server_factory)
    jobs = _JobStub()
    core = CoreToolRegistry(job_core_tools(cast(JobTools, jobs)))
    router = McpToolRouter(
        registry=PluginRegistry(
            AlwaysOnInstallations(StaticInstallationReader({}), source, catalog),
            DirectoryManifestSource(BUILTIN_PLUGINS_DIR),
        ),
        connector=McpConnector(server_factory=server_factory),
        audit=InMemoryToolAudit(),
        approvals=InMemoryApprovalService(),
        core_tools=core,
        config=RouterConfig(call_timeout_s=5, list_cache_s=0),
    )
    return router, jobs, core


async def run_chat(
    world: RunWorld, service: RunService, text: str = QUESTION
) -> list[dict[str, Any]]:
    record, _ = await service.start(NewRun(user_id=world.user, input_text=text, profile="chat"))
    events = await collect(service, world.user, record.id)
    await service.wait_finished(record.id)
    return events


async def test_news_summary_is_answered_in_chat_with_links_and_no_job(world: RunWorld) -> None:
    web = FakeWeb(NEWS)
    router, jobs, _ = build_router(web)
    final = answer(
        "Главное за сегодня: две новости.",
        display={
            "kind": "text",
            "markdown": "1. [Первая новость](https://example.com/a)\n"
            "2. [Вторая новость](https://example.org/b)",
        },
    )
    gateway = FakeModelGateway(
        [
            tool_step("c1", "builtin.internet.news", {"query": "главные новости"}),
            text_step(final),
        ]
    )
    service = world.service(gateway, router=router)

    events = await run_chat(world, service)

    # Search is offered without an installation; background.start is described as exceptional.
    offered = {spec.name: spec for spec in gateway.requests[0].tools}
    assert {"builtin.internet.search", "builtin.internet.news", "builtin.internet.read"} <= set(
        offered
    )
    assert "ONLY for long work" in offered["background.start"].description
    system = gateway.requests[0].messages[0].content
    assert "builtin.internet.news" in system and "Сегодня " in system

    # The answer carries the links; nothing ran in the background.
    response = payload_of(events, "response.final")["response"]
    assert "https://example.com/a" in response["display"]["markdown"]
    assert jobs.started == []
    assert "background.start" not in {
        call["tool"] for call in (e["payload"] for e in events if e["type"] == "tool.call.started")
    }
    assert web.calls == [("search", "главные новости", True)]
    assert types(events)[-1] == "run.completed"

    # The journal entry has the arguments.
    started = payload_of(events, "tool.call.started")
    assert started["tool"] == "builtin.internet.news"
    assert started["arguments"] == {"query": "главные новости"}
    completed = payload_of(events, "tool.call.completed")
    assert completed["status"] == "succeeded" and "error" not in completed
    # The model got the links as untrusted tool data.
    tool_message = json.loads(gateway.requests[1].messages[-1].content)
    assert "https://example.com/a" in tool_message["content"]


async def test_failed_search_journal_has_query_and_reason(world: RunWorld) -> None:
    web = FakeWeb()
    web.fail = "Источник недоступен или ограничивает автоматическое чтение."
    router, jobs, _ = build_router(web)
    gateway = FakeModelGateway(
        [
            tool_step("c1", "builtin.internet.news", {"query": "курс доллара"}),
            text_step(answer("Не удалось найти новости.")),
        ]
    )
    service = world.service(gateway, router=router)

    events = await run_chat(world, service, "курс доллара")

    completed = payload_of(events, "tool.call.completed")
    assert completed["status"] == "failed"
    assert completed["error"]["code"] == "tool_execution_failed"
    assert "«курс доллара»" in completed["error_detail"]
    assert "Источник недоступен" in completed["error_detail"]
    assert "<tool_result" not in completed["error_detail"]
    assert payload_of(events, "tool.call.started")["arguments"] == {"query": "курс доллара"}
    assert jobs.started == []


async def test_empty_search_tells_the_model_the_query(world: RunWorld) -> None:
    router, _, _ = build_router(FakeWeb([]))
    gateway = FakeModelGateway(
        [
            tool_step("c1", "builtin.internet.search", {"query": "несуществующее"}),
            text_step(answer("Ничего не нашлось.")),
        ]
    )
    service = world.service(gateway, router=router)

    await run_chat(world, service, "найди")

    result = json.loads(gateway.requests[1].messages[-1].content)["content"]
    assert "По запросу «несуществующее» ничего не найдено" in result


def test_background_start_card_is_reserved_for_long_work() -> None:
    (start,) = [
        t for t in job_core_tools(cast(JobTools, _JobStub())) if t.name == "background.start"
    ]
    assert isinstance(start, CoreTool)
    assert "Do NOT use it for questions" in start.description
    goal = start.input_schema["properties"]
    assert isinstance(goal, dict) and "own words" in str(goal["goal"])
