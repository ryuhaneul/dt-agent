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
