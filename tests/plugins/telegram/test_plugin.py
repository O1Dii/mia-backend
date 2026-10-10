"""The ``builtin.telegram`` package: manifest against the M00 contract, server surface and error
paths with a fake host (no database), and the production wiring."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from app.core.config import Settings
from app.main import create_app
from app.modules.plugins.builtin import BuiltinManifestSource, CallIdentity
from app.modules.plugins.install.grants import required_grants
from app.plugins.builtin.telegram.host import (
    BotLinkOffer,
    HandoffOutcome,
    HostUnavailableError,
    Recipient,
    bind_host,
    current_host,
)

from tests.fakes.contracts import assert_mvp_valid, validator_module
from tests.plugins.support import Caller, connect, make_services, meta, server_for

PLUGIN_ID = "builtin.telegram"
YANA = Recipient(ref="person:1", name="Яна Петрова", relation="жена")


@dataclass
class FakeHost:
    found: list[Recipient] = field(default_factory=lambda: [YANA])
    outcome: HandoffOutcome = field(
        default_factory=lambda: HandoffOutcome("opened", "Telegram открыт. НЕ отправлено.", True)
    )
    offer: BotLinkOffer = field(
        default_factory=lambda: BotLinkOffer(
            False, "https://t.me/b?start=SECRET", "2030-01-01T00:00:00Z"
        )
    )
    unavailable: str | None = None
    handoffs: list[tuple[Recipient, str]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    async def candidates(self, identity: CallIdentity, to: str) -> list[Recipient]:
        self.queries.append(to)
        if self.unavailable:
            raise HostUnavailableError(self.unavailable)
        return list(self.found)

    async def hand_off(
        self, identity: CallIdentity, recipient: Recipient, text: str
    ) -> HandoffOutcome:
        self.handoffs.append((recipient, text))
        return self.outcome

    async def bot_link(self, identity: CallIdentity) -> BotLinkOffer:
        if self.unavailable:
            raise HostUnavailableError(self.unavailable)
        return self.offer


@pytest.fixture
def host() -> Iterator[FakeHost]:
    fake = FakeHost()
    bind_host(fake)
    yield fake
    bind_host(None)


def plugin() -> dict[str, Any]:
    found = BuiltinManifestSource().get(PLUGIN_ID)
    assert found is not None
    return dict(found.manifest)


def test_manifest_is_valid_and_declares_the_approval_and_device_contract() -> None:
    manifest = plugin()
    assert_mvp_valid(manifest, "plugin-manifest")
    validator_module().check_manifest_semantics(manifest, PLUGIN_ID)
    assert manifest["server"] == {
        "kind": "builtin",
        "module": "app.plugins.builtin.telegram.server",
    }
    assert manifest["tools"]["send_message"]["risk"] == "write_external"
    assert manifest["tools"]["send_message"]["voice"] is True
    assert manifest["tools"]["connect_bot"]["risk"] == "write_reversible"
    assert not manifest["tools"]["connect_bot"].get("voice")
    assert manifest["requires"]["deviceTools"] == ["telegram.handoff"]
    # The device hand-off (25 s on the phone) must fit into the tool deadline.
    assert manifest["tools"]["send_message"]["timeoutSeconds"] >= 30
    assert required_grants(manifest) == {"tools.write_external", "tools.write_reversible"}
    found = BuiltinManifestSource().get(PLUGIN_ID)
    assert found is not None
    skill = found.skill_text()
    assert len(skill.splitlines()) <= 20
    assert "никогда не говори «отправила»" in skill.lower()


async def test_tools_list_equals_the_manifest() -> None:
    async with connect(server_for(PLUGIN_ID, make_services())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == set(plugin()["tools"])
    annotations = tools["send_message"].annotations
    assert annotations is not None and annotations.read_only_hint is False
    schema = tools["send_message"].input_schema
    assert set(schema["required"]) == {"to", "text"}


async def test_send_message_hands_the_text_to_the_host(host: FakeHost) -> None:
    async with connect(server_for(PLUGIN_ID, make_services())) as client:
        call = Caller(client, meta())
        data = await call("send_message", {"to": "Яна", "text": "Купи молоко"})
        assert data == {
            "recipient": "Яна Петрова",
            "opened": True,
            "sent": False,
            "direct_chat": True,
        }
        assert host.handoffs == [(YANA, "Купи молоко")]
        assert host.queries == ["Яна"]
        assert "НЕ отправлено" in await call.text("send_message", {"to": "Яна", "text": "x"})


async def test_unknown_or_ambiguous_recipient_never_reaches_the_phone(host: FakeHost) -> None:
    async with connect(server_for(PLUGIN_ID, make_services())) as client:
        call = Caller(client, meta())
        host.found = []
        assert "Не нашла" in await call.error("send_message", {"to": "Никто", "text": "x"})
        host.found = [YANA, Recipient(ref="contact:1", name="Яна Гейтова")]
        message = await call.error("send_message", {"to": "Яна", "text": "x"})
        assert "Яна Петрова, жена (person:1)" in message and "Яна Гейтова (contact:1)" in message
        assert "Ничего не отправлено" in message
        assert host.handoffs == []


async def test_failed_or_uncertain_handoff_is_a_tool_error(host: FakeHost) -> None:
    async with connect(server_for(PLUGIN_ID, make_services())) as client:
        call = Caller(client, meta())
        for status in ("failed", "uncertain"):
            host.outcome = HandoffOutcome(status, "Телефон заблокирован. Ничего не отправлено.")
            assert "заблокирован" in await call.error("send_message", {"to": "Яна", "text": "x"})


async def test_arguments_are_validated_by_the_schema(host: FakeHost) -> None:
    async with connect(server_for(PLUGIN_ID, make_services())) as client:
        call = Caller(client, meta())
        for arguments in ({"to": "", "text": "x"}, {"to": "Яна", "text": ""}, {"to": "Яна"}):
            assert (await call.raw("send_message", arguments)).is_error
        assert (await call.raw("send_message", {"to": "Яна", "text": "я" * 4001})).is_error
        assert host.handoffs == []


async def test_host_unavailable_and_missing_host_are_clear_errors(host: FakeHost) -> None:
    async with connect(server_for(PLUGIN_ID, make_services())) as client:
        call = Caller(client, meta())
        host.unavailable = "Нужен телефон."
        assert "Нужен телефон." in await call.error("send_message", {"to": "Яна", "text": "x"})
        assert "Нужен телефон." in await call.error("connect_bot")
        bind_host(None)
        assert "недоступен" in await call.error("send_message", {"to": "Яна", "text": "x"})


async def test_connect_bot_keeps_the_one_time_link_away_from_the_model(host: FakeHost) -> None:
    async with connect(server_for(PLUGIN_ID, make_services())) as client:
        call = Caller(client, meta())
        text = await call.text("connect_bot")
        assert "SECRET" not in text and "t.me" not in text
        assert "Открыть Telegram" in text
        result = await call.raw("connect_bot")
        # The link travels only as a screen action in _meta, not in structuredContent.
        assert result.structured_content == {"linked": False, "expires_at": "2030-01-01T00:00:00Z"}
        assert result.meta is not None and result.meta["mia/action"] == {
            "kind": "open_url",
            "url": "https://t.me/b?start=SECRET",
            "label": "Открыть Telegram",
            "expires_at": "2030-01-01T00:00:00Z",
        }
        host.offer = BotLinkOffer(already_linked=True)
        linked = await call.raw("connect_bot")
        assert linked.structured_content == {"linked": True}
        assert "mia/action" not in (linked.meta or {})


@pytest.mark.parametrize(
    "offer",
    [
        BotLinkOffer(False, None, None),
        BotLinkOffer(False, "http://t.me/b?start=SECRET", "2030-01-01T00:00:00Z"),
        BotLinkOffer(False, "https://t.me/b?start=SECRET", "tomorrow"),
    ],
)
async def test_connect_bot_without_a_usable_link_is_a_tool_error(
    host: FakeHost, offer: BotLinkOffer
) -> None:
    host.offer = offer
    async with connect(server_for(PLUGIN_ID, make_services())) as client:
        message = await Caller(client, meta()).error("connect_bot")
        assert "SECRET" not in message and "ссылку" in message


async def test_calls_without_identity_are_refused(host: FakeHost) -> None:
    async with connect(server_for(PLUGIN_ID, make_services())) as client:
        result = await client.call_tool("send_message", {"to": "Яна", "text": "x"})
        assert result.is_error and host.queries == []


def test_production_app_wires_the_bot_the_channel_and_the_plugin_host() -> None:
    bind_host(None)
    app = create_app(
        Settings(
            app_env="test",
            telegram_bot_token="123456:TEST-TOKEN-NOT-REAL",
            telegram_bot_username="mia_test_bot",
            telegram_webhook_secret="s3cret",
        )
    )
    paths = set(app.openapi()["paths"])
    assert {"/v1/telegram/link", "/webhooks/telegram"} <= paths
    assert app.state.telegram.configured
    assert app.state.jobs.notifications.channels.names() == ["push", "telegram"]
    assert current_host() is not None
    bind_host(None)


def test_without_a_bot_token_the_channel_stays_off() -> None:
    app = create_app(Settings(app_env="test"))
    assert not app.state.telegram.configured
    assert app.state.jobs.notifications.channels.names() == ["push"]
    bind_host(None)
