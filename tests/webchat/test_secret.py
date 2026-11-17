"""Secret input HTTP contracts, validation parity and absence from public sinks."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.infra.secret_store import list_keys, set_secret, validate_value
from tests.webchat.test_bridge_api import (
    conversation as conversation,  # noqa: PLC0414
)
from tests.webchat.test_bridge_api import headers, post_message
from tests.webchat.test_web_app import login
from tests.webchat.test_web_app import site as site  # noqa: PLC0414

VALUE = "synthetic-pr3-test-only"


async def start(h, command="/secret set SAMPLE_KEY", tid="-123.7"):
    response = await post_message(h, tid=tid, text=command)
    assert response.status == 200
    return await response.json()


async def step(h, token, *, value=VALUE, confirm=None, tid="-123.7"):
    action = "value" if confirm is None else "confirm"
    payload = (
        {"token": token, "value": value}
        if confirm is None
        else {"token": token, "confirm": confirm}
    )
    response = await h.client.post(
        f"/webchat/v1/topics/{tid}/secret/{action}", json=payload, headers=headers()
    )
    return response, await response.json()


async def assert_private(h, caplog, *tokens):
    history = await h.runtime.history.read("-123.7")
    outbox = await h.runtime.outbox.pending()
    visible = repr(history) + repr(outbox) + caplog.text + repr(h.calls)
    for sensitive in (VALUE, *tokens):
        assert sensitive not in visible
    assert not h.calls
    h.bot._orch.handle_message_streaming.assert_not_awaited()
    for item, entry in zip(outbox, history, strict=True):
        expected = (
            f"Command executed: {entry.text}" if entry.text.startswith("/secret") else entry.text
        )
        assert item.text == expected
    assert not list(h.paths.ductor_home.glob("*secret*state*"))


async def test_value_is_saved_directly_and_never_reaches_public_sinks(conversation, caplog):
    h = conversation
    caplog.set_level(logging.DEBUG)
    result = await start(h)
    assert result["secret"]["stage"] == "value"
    assert result["secret"]["message_key"] == "secret.prompt"
    token = result["secret"]["token"]
    response, saved = await step(h, token)
    assert response.status == 200
    assert saved["secret"]["stage"] == "saved"
    assert VALUE not in json.dumps(saved)
    assert list_keys(h.paths.ductor_home / "secrets.env") == ["SAMPLE_KEY"]
    assert (h.paths.ductor_home / "secrets.env").read_text() == f"SAMPLE_KEY={VALUE}\n"
    response, expired = await step(h, token)
    assert response.status == 409
    assert expired["secret"]["stage"] == "expired"
    await assert_private(h, caplog, token)


@pytest.mark.parametrize(
    "command",
    [
        " /SeCrEt@Test_Bot set SAMPLE_KEY " + VALUE,
        "/secret set SAMPLE_KEY\n" + VALUE,
        "/secret list " + VALUE,
        "/SECRET@bot " + VALUE,
        "/secret delete SAMPLE_KEY " + VALUE,
        "/secret set " + VALUE,
    ],
)
async def test_all_command_forms_are_sanitized_before_recording(conversation, caplog, command):
    h = conversation
    result = await start(h, command)
    assert result["secret"]["stage"] in {"error", "usage"}
    assert VALUE not in repr(result)
    from ductor_bot.webchat.commands import parse_command
    from ductor_bot.webchat.secret import WebSecrets

    parsed = parse_command(command)
    history = await h.runtime.history.read("-123.7")
    assert history[0].text == WebSecrets.safe_command(parsed[1])
    await assert_private(h, caplog)


@pytest.mark.parametrize(
    "value", ["", "x" * 4097, "a\0b", "a\nb", " a", "a ", "'abc", 'abc"', "$NAME"]
)
async def test_value_validation_matches_telegram_store(conversation, value):
    h = conversation
    result = await start(h)
    old = result["secret"]["token"]
    response, rejected = await step(h, old, value=value)
    assert response.status == 200
    assert rejected["secret"]["stage"] == "value"
    assert rejected["secret"]["reason"] == validate_value(value)
    assert rejected["secret"]["params"]["reason_code"] == validate_value(value)
    assert rejected["secret"]["params"]["max"] == 4096
    fresh = rejected["secret"]["token"]
    assert fresh != old
    assert (await step(h, old))[0].status == 409
    assert (await step(h, fresh))[1]["secret"]["stage"] == "saved"


async def test_overwrite_delete_confirm_cancel_and_list(conversation, caplog):
    h = conversation
    path = h.paths.ductor_home / "secrets.env"
    set_secret(path, "SAMPLE_KEY", "old-synthetic")
    listing = await start(h, "/secret list")
    assert listing["text"] == "Stored keys:\nSAMPLE_KEY"
    overwrite = await start(h)
    assert overwrite["secret"]["stage"] == "confirm_overwrite"
    token = overwrite["secret"]["token"]
    assert (await step(h, token))[0].status == 409
    assert (await step(h, token, confirm=True))[1]["secret"]["stage"] == "expired"
    overwrite = await start(h)
    token = overwrite["secret"]["token"]
    response, value = await step(h, token, confirm=True)
    assert response.status == 200
    fresh = value["secret"]["token"]
    assert fresh != token
    assert (await step(h, token, confirm=True))[0].status == 409
    assert (await step(h, fresh))[1]["secret"]["stage"] == "saved"
    deletion = await start(h, "/secret delete SAMPLE_KEY")
    assert deletion["secret"]["stage"] == "confirm_delete"
    response, cancelled = await step(h, deletion["secret"]["token"], confirm=False)
    assert response.status == 200
    assert cancelled["secret"]["stage"] == "cancelled"
    assert list_keys(path) == ["SAMPLE_KEY"]
    deletion = await start(h, "/secret delete SAMPLE_KEY")
    response, deleted = await step(h, deletion["secret"]["token"], confirm=True)
    assert deleted["secret"]["stage"] == "deleted"
    assert list_keys(path) == []
    assert (await start(h, "/secret list"))["secret"]["stage"] == "list"
    pending = await start(h)
    await start(h, "/secret cancel")
    assert (await step(h, pending["secret"]["token"]))[0].status == 409
    await assert_private(h, caplog, token, fresh)


async def test_expiry_and_new_request_invalidate_tokens(conversation):
    h = conversation
    with patch("ductor_bot.webchat.secret.time.time", return_value=1000):
        first = await start(h)
        second = await start(h)
    assert second["secret"]["expires_at"] == 1120
    assert second["secret"]["expires_in"] == 120
    with patch("ductor_bot.webchat.secret.time.time", return_value=1120):
        for result in (first, second):
            response, expired = await step(h, result["secret"]["token"])
            assert response.status == 409
            assert expired["secret"]["stage"] == "expired"
    assert not list_keys(h.paths.ductor_home / "secrets.env")


async def test_wrong_topic_does_not_consume_token(conversation):
    h = conversation
    from ductor_bot.session.key import SessionKey

    await h.bot._orch._sessions.resolve_session(SessionKey.telegram(-123, 8))
    pending = await start(h)
    token = pending["secret"]["token"]
    response, _ = await step(h, token, tid="-123.8")
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers
    assert (await step(h, token))[0].status == 200


@pytest.mark.parametrize("action", ["value", "confirm"])
async def test_browser_secret_relay_requires_session_csrf_and_custom_header(site, action):
    client, fake = site
    path = f"/api/topics/-123.7/secret/{action}"
    payload = (
        {"token": "synthetic-token", "value": VALUE}
        if action == "value"
        else {"token": "synthetic-token", "confirm": True}
    )
    response = await client.post(path, json=payload, headers={"Authorization": "Bearer ignored"})
    assert response.status == 403
    good, _ = await login(client)
    for missing in ("Cookie", "X-Dtw-Csrf", "X-Dtw-Client"):
        response = await client.post(
            path, json=payload, headers={k: v for k, v in good.items() if k != missing}
        )
        assert response.status == 403
        assert "WWW-Authenticate" not in response.headers
    response = await client.post(path, json=payload, headers=good)
    assert response.status == 200
    assert (await response.json())["body"] == payload
    assert fake.calls[-1][1] == path.removeprefix("/api")


@pytest.mark.parametrize("action", ["value", "confirm"])
async def test_bridge_secret_routes_require_custom_token(conversation, action):
    response = await conversation.client.post(
        f"/webchat/v1/topics/-123.7/secret/{action}",
        json={"token": "unknown", "value": VALUE},
        headers={"Authorization": "Bearer ignored"},
    )
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers


async def test_retry_keeps_original_two_minute_deadline(conversation):
    h = conversation
    with patch("ductor_bot.webchat.secret.time.time", return_value=1000):
        pending = await start(h)
    with patch("ductor_bot.webchat.secret.time.time", return_value=1119):
        _, rejected = await step(h, pending["secret"]["token"], value=" ")
    assert rejected["secret"]["expires_at"] == 1120
    assert rejected["secret"]["expires_in"] == 1
    with patch("ductor_bot.webchat.secret.time.time", return_value=1120):
        response, expired = await step(h, rejected["secret"]["token"])
    assert response.status == 409
    assert expired["secret"]["stage"] == "expired"


async def test_store_failure_does_not_expose_exception_or_lose_retry(conversation, caplog):
    h = conversation
    pending = await start(h)
    with patch("ductor_bot.webchat.secret.set_secret", side_effect=OSError(VALUE)):
        response, failed = await step(h, pending["secret"]["token"])
    assert response.status == 200
    assert failed["secret"]["stage"] == "value"
    assert failed["secret"]["message_key"] == "secret.save_failed"
    assert (await step(h, failed["secret"]["token"]))[1]["secret"]["stage"] == "saved"
    await assert_private(h, caplog, pending["secret"]["token"], failed["secret"]["token"])


async def test_concurrent_token_use_saves_once(conversation):
    import asyncio

    h = conversation
    pending = await start(h)
    token = pending["secret"]["token"]
    responses = await asyncio.gather(step(h, token), step(h, token))
    assert sorted(response.status for response, _ in responses) == [200, 409]
    history = await h.runtime.history.read("-123.7")
    assert sum("saved (length:" in entry.text for entry in history) == 1


async def test_public_event_and_disk_records_never_contain_value_or_token(conversation):
    h = conversation
    queue = h.runtime.events.subscribe()
    try:
        pending = await start(h)
        token = pending["secret"]["token"]
        await step(h, token)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        public = repr(events)
        for path in h.paths.chat_log_dir.rglob("*"):
            if path.is_file():
                public += path.read_text()
        response = await h.client.get("/webchat/v1/topics/-123.7/messages", headers=headers())
        public += await response.text()
        assert VALUE not in public
        assert token not in public
    finally:
        h.runtime.events.unsubscribe(queue)


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"token": "unknown", "value": 1}, {"token": 3, "value": VALUE}]
)
async def test_malformed_value_requests_are_400(conversation, payload):
    response = await conversation.client.post(
        "/webchat/v1/topics/-123.7/secret/value",
        data=json.dumps(payload),
        headers={**headers(), "Content-Type": "application/json"},
    )
    assert response.status == 400
    assert VALUE not in await response.text()


@pytest.mark.parametrize("confirm", ["true", 1, None, [], {}])
async def test_confirmation_requires_a_boolean(conversation, confirm):
    response = await conversation.client.post(
        "/webchat/v1/topics/-123.7/secret/confirm",
        json={"token": "unknown", "confirm": confirm},
        headers=headers(),
    )
    assert response.status == 400


async def test_restart_forgets_tokens(tmp_path):
    from ductor_bot.webchat.secret import WebSecrets

    first = WebSecrets(tmp_path, tmp_path / "secrets.env", "en")
    pending = await first.command("1.7", "/secret set SAMPLE_KEY")
    restarted = WebSecrets(tmp_path, tmp_path / "secrets.env", "en")
    status, result = await restarted.complete("1.7", pending["secret"]["token"], "value", VALUE)
    assert status == 409
    assert result["secret"]["stage"] == "expired"
    assert not (tmp_path / "secrets.env").exists()


async def test_web_commands_and_value_errors_use_telegram_text(tmp_path):
    from ductor_bot.i18n import init
    from ductor_bot.messenger.telegram.secret_input import SecretInput
    from ductor_bot.webchat.secret import WebSecrets
    from tests.messenger.telegram.test_secret_input import FakeBot, message

    init("en")
    network = FakeBot()
    telegram = SecretInput(
        network,
        is_main=True,
        allowed_users=[101],
        ductor_home=tmp_path / "tg",
        store_path=tmp_path / "tg" / "secrets.env",
    )
    secret = WebSecrets(tmp_path / "web", tmp_path / "web" / "secrets.env", "en")
    try:
        for command in ["/secret", "/secret list", "/secret set bad", "/secret cancel"]:
            await telegram._command(message(command), command.split(), 101)
            result = await secret.command("101", command)
            assert result["text"] == network.sent[-1][0].text
        await telegram._request(101, 101, "SAMPLE_KEY")
        pending = await secret.command("101", "/secret set SAMPLE_KEY")
        for value in [" ", "a\nb", "$NAME", VALUE]:
            await telegram._value(message(value), telegram._pending[(101, 101)])
            _, result = await secret.complete("101", pending["secret"]["token"], "value", value)
            assert result["text"] == network.sent[-1][0].text
            pending = result
    finally:
        await telegram.close()


async def test_telegram_mirror_sender_receives_only_sanitized_text(conversation, caplog):
    from ductor_bot.chat_log.link import OutboxDrainer
    from ductor_bot.messenger.telegram.sender import SendProgress

    h = conversation
    pending = await start(h)
    token = pending["secret"]["token"]
    await step(h, token)
    entries = await h.runtime.history.read("-123.7")
    ops = SimpleNamespace(
        send_text=AsyncMock(return_value=SendProgress(True, 1, None, "", None)),
        keyboard_observer=MagicMock(),
    )
    link = SimpleNamespace(
        state=lambda: {"state": "up"}, probe_failure_generation=0, record=MagicMock()
    )
    drainer = OutboxDrainer(h.runtime.outbox, ops, link, history=h.runtime.history)
    await drainer.drain()
    mirrored = [call.args[2] for call in ops.send_text.await_args_list]
    assert entries[0].text == "/secret set SAMPLE_KEY"
    assert mirrored[1:] == [entry.text for entry in entries[1:]]
    assert mirrored[0] == "Command executed: /secret set SAMPLE_KEY"
    assert VALUE not in repr(ops.send_text.await_args_list) + caplog.text
    assert token not in repr(ops.send_text.await_args_list) + caplog.text
    assert await h.runtime.outbox.pending() == []


@pytest.mark.parametrize(("tid", "status"), [("unknown", 409), ("invalid", 404), ("999.9", 404)])
async def test_secret_step_rejects_missing_or_read_only_topics(conversation, tid, status):
    response, _ = await step(conversation, "unknown", tid=tid)
    assert response.status == status


async def test_env_note_metadata_preserves_original_response_text(conversation):
    h = conversation
    (h.paths.ductor_home / ".env").write_text("SAMPLE_KEY=synthetic-existing-value\n")
    pending = await start(h)
    response, saved = await step(h, pending["secret"]["token"])
    assert response.status == 200
    assert saved["secret"]["params"]["env_note"] is True
    assert saved["text"] == (
        f"SAMPLE_KEY saved (length: {len(VALUE)} characters).\n"
        "SAMPLE_KEY also exists in .env. CLI provider keys use the .env value."
    )
