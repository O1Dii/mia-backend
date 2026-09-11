from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.modules.realtime.contracts import (
    INPUT_AUDIO_FORMAT,
    OUTPUT_AUDIO_FORMAT,
    ConfigurePayload,
)
from app.modules.realtime.sessions import RealtimeSession, RealtimeSessionStatus
from app.providers.ai.gemini_live import GeminiLiveAdapter, GeminiLiveConfig
from pydantic import SecretStr


async def test_real_gemini_live_connection_when_process_credentials_are_configured() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    model = os.environ.get("GEMINI_LIVE_MODEL")
    if not api_key or not model:
        pytest.skip("GEMINI_API_KEY and GEMINI_LIVE_MODEL are not configured in the process")

    conversation_id = uuid4()
    session = RealtimeSession(
        id=uuid4(),
        user_id=uuid4(),
        conversation_id=conversation_id,
        locale="en-US",
        input_audio=INPUT_AUDIO_FORMAT,
        output_audio=OUTPUT_AUDIO_FORMAT,
        expires_at=datetime.now(UTC) + timedelta(minutes=2),
        status=RealtimeSessionStatus.CONNECTED,
    )
    adapter = GeminiLiveAdapter(
        session,
        GeminiLiveConfig(
            api_key=SecretStr(api_key),
            model=model,
            system_instruction="Reply briefly in English.",
            request_timeout_seconds=30.0,
        ),
    )
    try:
        await adapter.configure(
            ConfigurePayload(
                protocol_version=1,
                conversation_id=conversation_id,
                locale="en-US",
                input_audio=(INPUT_AUDIO_FORMAT,),
                output_audio=(OUTPUT_AUDIO_FORMAT,),
                capabilities=("barge_in", "input_transcript", "output_transcript"),
            )
        )
        await adapter.send_audio(bytes(1280))
        await adapter.commit_audio(stream_id=uuid4(), turn_id=uuid4())
    finally:
        await adapter.close()
