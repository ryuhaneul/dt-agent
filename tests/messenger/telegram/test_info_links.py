"""Public project links and installed distribution identity."""

from unittest.mock import AsyncMock, patch

from ductor_bot.infra.version import get_current_version
from tests.messenger.telegram.test_app import _make_message, _make_tg_bot


async def test_info_links_point_to_the_public_project():
    tg, _ = _make_tg_bot()
    message = _make_message(chat_id=100)
    with patch("ductor_bot.messenger.telegram.app.send_rich", new_callable=AsyncMock) as send:
        await tg._on_info(message)
    keyboard = send.await_args.args[3].reply_markup
    assert [(button.text, button.url) for row in keyboard.inline_keyboard for button in row] == [
        ("GitHub", "https://github.com/ryuhaneul/dt-agent"),
        ("Changelog", "https://github.com/ryuhaneul/dt-agent/releases"),
    ]


def test_current_version_uses_the_public_distribution():
    with patch("ductor_bot.infra.version.importlib.metadata.version", return_value="1.0.0") as version:
        assert get_current_version() == "1.0.0"
    version.assert_called_once_with("dt-agent")


async def test_info_builder_matches_telegram_text_and_keyboard():
    from ductor_bot.i18n import t
    from ductor_bot.messenger.telegram.app import build_info_response
    from ductor_bot.text.response_format import SEP, fmt

    tg, _ = _make_tg_bot()
    with patch("ductor_bot.messenger.telegram.app.get_current_version", return_value="1.2.3"):
        text, keyboard = build_info_response()
        with patch("ductor_bot.messenger.telegram.app.send_rich", new_callable=AsyncMock) as send:
            await tg._on_info(_make_message(chat_id=100, topic_thread_id=7))
    assert text == fmt(
        t("info.header"), t("info.version", version="1.2.3"), SEP, t("info.telegram_description")
    )
    assert send.call_args.args[2] == text
    assert send.call_args.args[3].reply_markup.model_dump() == keyboard.model_dump()
    assert send.call_args.args[3].thread_id == 7
