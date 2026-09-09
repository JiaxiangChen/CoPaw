# -*- coding: utf-8 -*-
"""Stable persisted history message identity coverage."""

from agentscope.message import Msg

from swe.app.runner.utils import agentscope_msg_to_message, legacy_message_id


def test_history_conversion_preserves_raw_message_id() -> None:
    raw = Msg(
        name="user",
        role="user",
        content="question",
        timestamp="2026-08-26T01:00:00Z",
    )
    raw.id = "raw-user-1"

    first = agentscope_msg_to_message([raw], session_id="session-1")
    second = agentscope_msg_to_message([raw], session_id="session-1")

    assert [message.id for message in first] == ["raw-user-1"]
    assert [message.id for message in second] == ["raw-user-1"]
    assert first[0].metadata["original_id"] == "raw-user-1"


def test_missing_raw_id_uses_deterministic_legacy_identity() -> None:
    raw = Msg(
        name="user",
        role="user",
        content="question",
        timestamp="2026-08-26T01:00:00Z",
    )
    raw.id = ""

    first = agentscope_msg_to_message([raw], session_id="session-1")
    second = agentscope_msg_to_message([raw], session_id="session-1")

    expected = legacy_message_id(
        "session-1",
        0,
        raw.timestamp,
        raw.role,
        raw.content,
    )
    assert first[0].id == second[0].id == expected
    assert first[0].id.startswith("legacy:")


def test_history_conversion_preserves_flat_attachment_fields() -> None:
    raw = Msg(
        name="user",
        role="user",
        content=[
            {"type": "text", "text": "请分析附件"},
            {
                "type": "file",
                "file_url": "/tmp/report.pdf",
                "file_name": "报告.pdf",
            },
            {"type": "image", "image_url": "/tmp/screenshot.png"},
            {
                "type": "audio",
                "audio_url": "/tmp/recording.mp3",
                "format": "mp3",
            },
            {"type": "video", "video_url": "/tmp/demo.mp4"},
        ],
    )

    message = agentscope_msg_to_message(raw, session_id="session-1")[0]
    content = [item.model_dump(mode="json") for item in message.content]

    assert content[0]["text"] == "请分析附件"
    assert content[1]["file_url"] == "/tmp/report.pdf"
    assert content[1]["filename"] == "报告.pdf"
    assert content[2]["image_url"] == "/tmp/screenshot.png"
    assert content[3]["data"] == "/tmp/recording.mp3"
    assert content[3]["format"] == "mp3"
    assert content[4]["video_url"] == "/tmp/demo.mp4"
