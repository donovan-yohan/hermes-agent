from gateway.browser_bridge import build_browser_chat_message, build_browser_context_message


def test_context_message_enables_live_browser_actions_for_explicit_requests():
    message = build_browser_context_message(
        {
            "note": "Open github and navigate to issues.",
            "url": "https://example.com",
            "title": "Example",
        }
    )
    assert "explicitly asking for a live browser action" in message
    assert "Execute the requested browser navigation/action first." in message
    assert "Do not preempt that explicit browser action with memory/worldview file work" in message


def test_context_message_keeps_no_live_recheck_default_for_reference_turns():
    message = build_browser_context_message(
        {
            "note": "Summarize this page and extract action items.",
            "url": "https://example.com",
            "title": "Example",
        }
    )
    assert "Do not call browser navigation/snapshot/vision tools" in message
    assert "explicitly asking for a live browser action" not in message


def test_slash_command_passthrough_ignores_page_context_wrapper():
    message = build_browser_chat_message(
        "/status",
        {
            "url": "https://example.com",
            "title": "Example",
            "note": "stale note should be ignored for slash mode",
        },
    )
    assert message == "/status"


def test_context_message_includes_discord_thread_excerpt_and_image_refs():
    message = build_browser_context_message(
        {
            "url": "https://discord.com/channels/1/2",
            "title": "Hermes Support",
            "metadata": {
                "discordThreadTitle": "ffmpeg stitching issue",
                "discordThreadMessageCount": 3,
                "discordThreadImageCount": 1,
                "discordThreadSource": "sidebar-thread",
            },
            "discordThreadText": "user\nAudio cuts out after 4k chars",
            "discordThreadImages": [
                {
                    "url": "https://media.discordapp.net/attachments/123/screenshot.png",
                    "alt_text": "Waveform screenshot",
                }
            ],
        }
    )

    assert "Discord thread excerpt:" in message
    assert "Audio cuts out after 4k chars" in message
    assert "- discordThreadTitle: ffmpeg stitching issue" in message
    assert "Discord thread image references:" in message
    assert "Waveform screenshot" in message
