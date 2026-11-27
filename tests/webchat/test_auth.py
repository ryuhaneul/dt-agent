"""Password, TOTP, throttling and credential CLI contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import stat
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ductor_bot.infra.env_secrets import load_env_secrets
from ductor_bot.infra.secret_store import set_secret
from ductor_bot.webchat import auth, cli
from ductor_bot.webchat.auth import (
    PASSWORD_KEY,
    TOTP_KEY,
    AuthError,
    BridgeAuth,
    hash_password,
    normalize_seed,
    totp_code,
    verify_password,
)
from ductor_bot.webchat.credentials import CredentialStore, decrypt_seed
from ductor_bot.workspace.paths import DuctorPaths

_PASSWORD = "sample-password-123"
_SEED = base64.b32encode(b"12345678901234567890").decode()
_IP = "198.51.100.7"


@pytest.fixture
async def service(tmp_path, monkeypatch):
    clock = SimpleNamespace(wall=1_800_000_000, mono=1000)
    monkeypatch.setattr(
        auth, "time", SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono)
    )
    store = tmp_path / "secrets.env"
    set_secret(store, PASSWORD_KEY, hash_password(_PASSWORD))
    state_path = tmp_path / "webchat" / "auth_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "seed_fingerprint": hashlib.sha256(_SEED.encode()).hexdigest(),
                "last_step": clock.wall // 30 - 2,
                "source": "login",
            }
        )
    )
    return BridgeAuth(store, "chat.example.com", state_path), clock


async def password_login(service, ip=_IP):
    instance, _ = service
    return await instance.handle("password", {"password": _PASSWORD, "client_ip": ip})


async def invalid(instance, action, data, code="invalid"):
    with pytest.raises(AuthError) as caught:
        await instance.handle(action, data)
    assert caught.value.payload["error"] == code
    return caught.value.payload


def test_password_format_and_verification():
    encoded = hash_password(_PASSWORD)
    assert re.fullmatch(r"scrypt:32768:8:1:[0-9a-f]{32}:[0-9a-f]{64}", encoded)
    assert verify_password(_PASSWORD, encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("\ud800", encoded)
    assert hash_password(_PASSWORD) != encoded
    for malformed in ("", "scrypt:2:8:1:00:00", encoded + "extra", "invalid"):
        assert not verify_password(_PASSWORD, malformed)


def test_rfc_vector():
    assert totp_code(_SEED, 59 // 30) == "287082"


@pytest.mark.parametrize("offset", [-1, 0, 1])
async def test_totp_window_and_reuse(service, offset):
    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    first = await password_login(service)
    assert first["next"] == "totp"
    code = totp_code(_SEED, clock.wall // 30 + offset)
    assert await instance.handle(
        "totp", {"preauth": first["preauth"], "code": code, "client_ip": _IP}
    ) == {"ok": True}
    second = await password_login(service)
    await invalid(instance, "totp", {"preauth": second["preauth"], "code": code, "client_ip": _IP})
    # Consumed preauth cannot be reused with a later code either.
    await invalid(instance, "totp", {"preauth": first["preauth"], "code": code, "client_ip": _IP})


async def test_expired_preauth_and_wrong_ip(service):
    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    first = await password_login(service)
    code = totp_code(_SEED, clock.wall // 30)
    await invalid(
        instance, "totp", {"preauth": first["preauth"], "code": code, "client_ip": "203.0.113.10"}
    )
    clock.mono += 300
    await invalid(instance, "totp", {"preauth": first["preauth"], "code": code, "client_ip": _IP})


async def test_totp_rejects_old_and_malformed_codes(service):
    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    first = await password_login(service)
    for code in (totp_code(_SEED, clock.wall // 30 - 2), "12345", "abcdef", 123456):
        await invalid(
            instance, "totp", {"preauth": first["preauth"], "code": code, "client_ip": _IP}
        )


async def test_totp_concurrent_replay(service):
    import asyncio

    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    first, second = await password_login(service), await password_login(service)
    code = totp_code(_SEED, clock.wall // 30)
    results = await asyncio.gather(
        *(
            instance.handle("totp", {"preauth": pre["preauth"], "code": code, "client_ip": _IP})
            for pre in (first, second)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, AuthError) for result in results) == 1
    assert sum(result == {"ok": True} for result in results) == 1


async def test_ip_lock_expires_and_other_addresses_do_not_lock_login(service):
    instance, clock = service
    for _ in range(5):
        await invalid(instance, "password", {"password": "wrong", "client_ip": _IP})
    locked = await invalid(
        instance, "password", {"password": _PASSWORD, "client_ip": _IP}, "locked"
    )
    assert locked["retry_after"] == 900
    await password_login(service, "203.0.113.10")
    clock.mono += 900
    await password_login(service)
    for index in range(20):
        await invalid(
            instance, "password", {"password": "wrong", "client_ip": f"203.0.113.{index + 1}"}
        )
    await password_login(service)


async def test_failures_combined_across_auth_stages(service):
    instance, _ = service
    first = await password_login(service)
    for _ in range(2):
        await invalid(instance, "password", {"password": "wrong", "client_ip": _IP})
    for _ in range(2):
        await invalid(
            instance, "totp", {"preauth": first["preauth"], "code": "bad", "client_ip": _IP}
        )
    await invalid(
        instance, "setup/confirm", {"preauth": first["preauth"], "code": "bad", "client_ip": _IP}
    )
    await invalid(instance, "password", {"password": _PASSWORD, "client_ip": _IP}, "locked")


async def test_success_resets_ip_failures(service):
    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    for index in range(4):
        for _ in range(4):
            await invalid(
                instance, "password", {"password": "wrong", "client_ip": f"203.0.113.{index + 1}"}
            )
        ip = f"203.0.113.{index + 1}"
        pre = await password_login(service, ip)
        await instance.handle(
            "totp",
            {
                "preauth": pre["preauth"],
                "code": totp_code(_SEED, clock.wall // 30),
                "client_ip": ip,
            },
        )
        assert ip not in instance._ip_failures
        clock.wall += 30


async def test_setup_new_paste_confirm_and_read_only_state(service):
    instance, clock = service
    instance.state_path.unlink()
    assert not instance.state_path.exists()
    assert instance.state() == {"password_configured": True, "totp_configured": False}
    first = await password_login(service)
    assert first["next"] == "setup"
    await invalid(instance, "setup/begin", {"mode": "new"})
    new = await instance.handle("setup/begin", {"preauth": first["preauth"], "mode": "new"})
    assert len(base64.b32decode(new["secret"])) == 20
    assert new["otpauth_uri"].startswith("otpauth://totp/dt-agent:chat.example.com?")
    assert not instance.state()["totp_configured"]
    pasted = await instance.handle(
        "setup/begin",
        {
            "preauth": first["preauth"],
            "mode": "paste",
            "secret": " ".join(_SEED.lower()[i : i + 4] for i in range(0, len(_SEED), 4)),
        },
    )
    assert pasted["secret"] == _SEED
    assert await instance.handle(
        "setup/confirm",
        {"preauth": first["preauth"], "code": totp_code(_SEED, clock.wall // 30), "client_ip": _IP},
    ) == {"ok": True}
    saved = instance.credential_store.load()
    assert saved is not None
    assert saved.totp is not None
    assert decrypt_seed(saved.totp, _PASSWORD)[0] == _SEED
    assert TOTP_KEY not in load_env_secrets(instance.store)
    assert stat.S_IMODE(instance.credential_store.path.stat().st_mode) == 0o600
    for action in ("setup/begin", "setup/confirm"):
        await invalid(
            instance, action, {"preauth": first["preauth"], "mode": "new"}, "totp_configured"
        )


@pytest.mark.parametrize("seed", ["ABC", "!!!!!!!!!!!!!!!!!", "A" * 17, "AAAA====AAAAAAAA"])
async def test_bad_seed_never_persisted(service, seed):
    instance, _ = service
    first = await password_login(service)
    await invalid(
        instance,
        "setup/begin",
        {"preauth": first["preauth"], "mode": "paste", "secret": seed},
        "bad_secret",
    )
    assert not instance.state()["totp_configured"]


def test_seed_normalization():
    assert normalize_seed(_SEED.lower()) == _SEED


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    paths = DuctorPaths(ductor_home=tmp_path)
    monkeypatch.setattr(cli, "resolve_paths", lambda: paths)
    monkeypatch.setattr(cli, "store_path", lambda _home: tmp_path / "secrets.env")
    return paths


@pytest.mark.parametrize(
    ("password", "confirmation"), [("short", "short"), (_PASSWORD, "different")]
)
def test_cli_rejects_password(cli_home, capsys, password, confirmation):
    with (
        patch.object(cli.getpass, "getpass", side_effect=[password, confirmation]),
        pytest.raises(SystemExit) as caught,
    ):
        cli.cmd_webchat(["set-password"])
    assert caught.value.code == 1
    assert not (cli_home.ductor_home / "secrets.env").exists()
    assert password not in capsys.readouterr().out


def test_cli_set_reset_and_clear(cli_home, capsys):
    store = cli_home.ductor_home / "secrets.env"
    set_secret(store, "OTHER_KEY", "other-test-value")
    with patch.object(cli.getpass, "getpass", side_effect=[_PASSWORD, _PASSWORD]):
        cli.cmd_webchat(["webchat", "set-password"])
    saved = CredentialStore(cli_home.webchat_dir / "credentials.json").load()
    assert saved is not None
    assert verify_password(_PASSWORD, saved.password_hash)
    assert saved.totp is None
    state_path = cli_home.webchat_dir / "auth_state.json"
    password_nonce = json.loads(state_path.read_text())["credential_nonce"]
    set_secret(store, TOTP_KEY, _SEED)
    cli.cmd_webchat(["reset-totp"])
    assert TOTP_KEY not in load_env_secrets(store)
    reset_nonce = json.loads(state_path.read_text())["credential_nonce"]
    assert reset_nonce != password_nonce
    cli.cmd_webchat(["clear-credentials"])
    assert json.loads(state_path.read_text())["credential_nonce"] not in {
        password_nonce,
        reset_nonce,
    }
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert load_env_secrets(store) == {"OTHER_KEY": "other-test-value"}
    output = capsys.readouterr().out
    assert _PASSWORD not in output
    assert _SEED not in output


def test_check_config_json_contract(cli_home, capsys):
    cli.cmd_webchat(["check-config", "--json"])
    body = json.loads(capsys.readouterr().out)
    assert body["errors"] == []
    assert body["chat_log"] == {"enabled": False}
    from ductor_bot.config import WebchatConfig

    assert body["webchat"] == WebchatConfig().model_dump(mode="json")
    assert not (cli_home.ductor_home / "webchat").exists()
    cli_home.config_path.parent.mkdir(parents=True, exist_ok=True)
    cli_home.config_path.write_text(json.dumps({"webchat": {"enabled": True}}))
    with pytest.raises(SystemExit) as caught:
        cli.cmd_webchat(["check-config", "--json"])
    assert caught.value.code == 1
    assert "chat_log_required" in json.loads(capsys.readouterr().out)["errors"]


@pytest.mark.parametrize(
    "args", [[], ["unknown"], ["set-password", "extra"], ["check-config", "--bad"]]
)
def test_cli_usage(args):
    with pytest.raises(SystemExit) as caught:
        cli.cmd_webchat(args)
    assert caught.value.code == 2


def test_root_webchat_dispatch(monkeypatch):
    from ductor_bot import __main__

    with (
        patch("ductor_bot.i18n._store", None),
        patch.object(__main__, "_cmd_webchat") as command,
    ):
        monkeypatch.setattr(__main__.sys, "argv", ["ductor", "webchat", "check-config", "--json"])
        __main__.main()
    command.assert_called_once_with(["webchat", "check-config", "--json"])


async def test_setup_invalid_code_does_not_save_or_consume_preauth(service):
    instance, clock = service
    first = await password_login(service)
    setup = await instance.handle("setup/begin", {"preauth": first["preauth"], "mode": "new"})
    await invalid(
        instance, "setup/confirm", {"preauth": first["preauth"], "code": "bad", "client_ip": _IP}
    )
    assert not instance.state()["totp_configured"]
    assert await instance.handle(
        "setup/confirm",
        {
            "preauth": first["preauth"],
            "code": totp_code(setup["secret"], clock.wall // 30),
            "client_ip": _IP,
        },
    ) == {"ok": True}


async def test_time_machine_lock_expiration(tmp_path, monkeypatch):
    import time

    import time_machine

    with time_machine.travel("2026-01-01T00:00:00Z", tick=False) as traveller:
        # Map only the service clock onto simulated wall time; keep the event loop's
        # monotonic clock intact so thread work and deadlines still run normally.
        monkeypatch.setattr(auth, "time", SimpleNamespace(time=time.time, monotonic=time.time))
        store = tmp_path / "secrets.env"
        set_secret(store, PASSWORD_KEY, hash_password(_PASSWORD))
        instance = BridgeAuth(store, "chat.example.com", tmp_path / "webchat" / "auth_state.json")
        for _ in range(5):
            await invalid(instance, "password", {"password": "wrong", "client_ip": _IP})
        traveller.shift(899)
        locked = await invalid(
            instance, "password", {"password": _PASSWORD, "client_ip": _IP}, "locked"
        )
        assert locked["retry_after"] == 1
        traveller.shift(1)
        assert (await instance.handle("password", {"password": _PASSWORD, "client_ip": _IP}))[
            "next"
        ] == "setup"


async def test_configured_setup_confirm_failures_also_count(service):
    instance, _ = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    for _ in range(5):
        await invalid(
            instance, "setup/confirm", {"client_ip": _IP, "code": "bad"}, "totp_configured"
        )
    await invalid(instance, "password", {"password": _PASSWORD, "client_ip": _IP}, "locked")


async def test_review_password_success_does_not_clear_totp_failures(service):
    instance, _ = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    for _ in range(5):
        pre = await password_login(service)
        await invalid(
            instance, "totp", {"preauth": pre["preauth"], "code": "bad", "client_ip": _IP}
        )
    await invalid(instance, "password", {"password": _PASSWORD, "client_ip": _IP}, "locked")


async def test_review_totp_replay_survives_restart(service):
    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    pre = await password_login(service)
    data = {"preauth": pre["preauth"], "code": totp_code(_SEED, clock.wall // 30), "client_ip": _IP}
    await instance.handle("totp", data)
    assert load_env_secrets(instance.store) == {}
    assert instance.credential_store.path.is_file()
    assert "DT_WEBCHAT_TOTP_LAST_USED" not in load_env_secrets(instance.store)
    state_path = instance.store.parent / "webchat" / "auth_state.json"
    assert state_path.is_file()
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    restarted = BridgeAuth(instance.store, instance.domain, instance.state_path)
    pre = await restarted.handle("password", {"password": _PASSWORD, "client_ip": _IP})
    await invalid(restarted, "totp", {**data, "preauth": pre["preauth"]})


async def test_review_setup_replay_is_distinct_and_does_not_count(service):
    instance, clock = service
    pre = await password_login(service)
    await instance.handle(
        "setup/begin", {"preauth": pre["preauth"], "mode": "paste", "secret": _SEED}
    )
    code = totp_code(_SEED, clock.wall // 30)
    await instance.handle(
        "setup/confirm", {"preauth": pre["preauth"], "code": code, "client_ip": _IP}
    )
    instance = BridgeAuth(instance.store, instance.domain, instance.state_path)
    pre = await instance.handle("password", {"password": _PASSWORD, "client_ip": _IP})
    for _ in range(6):
        await invalid(
            instance,
            "totp",
            {"preauth": pre["preauth"], "code": code, "client_ip": _IP},
            "code_reused",
        )
    assert instance._ip_failures.get(_IP, []) == []
    clock.wall += 30
    assert (
        await instance.handle(
            "totp",
            {
                "preauth": pre["preauth"],
                "code": totp_code(_SEED, clock.wall // 30),
                "client_ip": _IP,
            },
        )
    )["ok"]


@pytest.mark.parametrize("contents", [None, "{broken", "{}", '{"last_step": true}'])
async def test_runtime_state_missing_or_corrupt_blocks_live_window(service, contents, caplog):
    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    state_path = instance.store.parent / "webchat" / "auth_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if contents is None:
        state_path.unlink(missing_ok=True)
    else:
        state_path.write_text(contents)
    pre = await password_login(service)
    code = totp_code(_SEED, clock.wall // 30)
    await invalid(instance, "totp", {"preauth": pre["preauth"], "code": code, "client_ip": _IP})
    assert len([r for r in caplog.records if r.name == auth.__name__]) == 1
    restarted = BridgeAuth(instance.store, instance.domain, instance.state_path)
    pre = await restarted.handle("password", {"password": _PASSWORD, "client_ip": _IP})
    for offset in (-1, 0, 1):
        await invalid(
            restarted,
            "totp",
            {
                "preauth": pre["preauth"],
                "code": totp_code(_SEED, clock.wall // 30 + offset),
                "client_ip": _IP,
            },
        )
    clock.wall += 60
    assert (
        await restarted.handle(
            "totp",
            {
                "preauth": pre["preauth"],
                "code": totp_code(_SEED, clock.wall // 30),
                "client_ip": _IP,
            },
        )
    )["ok"]
    assert _SEED not in caplog.text
    assert code not in caplog.text
    assert len([r for r in caplog.records if r.name == auth.__name__]) == 1


async def test_runtime_state_reset_allows_immediate_enrollment(service, cli_home):
    instance, clock = service
    store = cli_home.ductor_home / "secrets.env"
    set_secret(store, PASSWORD_KEY, hash_password(_PASSWORD))
    set_secret(store, TOTP_KEY, _SEED)
    state_path = cli_home.webchat_dir / "auth_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "seed_fingerprint": hashlib.sha256(_SEED.encode()).hexdigest(),
                "last_step": clock.wall // 30 + 1,
                "source": "login",
            }
        )
    )
    cli.cmd_webchat(["reset-totp"])
    reset = BridgeAuth(store, instance.domain, state_path)
    pre = await reset.handle("password", {"password": _PASSWORD, "client_ip": _IP})
    await reset.handle("setup/begin", {"preauth": pre["preauth"], "mode": "paste", "secret": _SEED})
    assert (
        await reset.handle(
            "setup/confirm",
            {
                "preauth": pre["preauth"],
                "code": totp_code(_SEED, clock.wall // 30),
                "client_ip": _IP,
            },
        )
    )["ok"]
    assert json.loads(state_path.read_text())["source"] == "setup"
    assert "DT_WEBCHAT_TOTP_LAST_USED" not in load_env_secrets(store)


async def test_runtime_state_ignores_legacy_secret_value(service):
    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    set_secret(instance.store, "DT_WEBCHAT_TOTP_LAST_USED", "legacy-value")
    pre = await password_login(service)
    assert (
        await instance.handle(
            "totp",
            {
                "preauth": pre["preauth"],
                "code": totp_code(_SEED, clock.wall // 30),
                "client_ip": _IP,
            },
        )
    )["ok"]
    assert load_env_secrets(instance.store) == {"DT_WEBCHAT_TOTP_LAST_USED": "legacy-value"}
    assert instance.credential_store.path.is_file()


async def test_review_preauth_rejected_after_password_change(service):
    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    pre = await password_login(service)
    set_secret(instance.store, PASSWORD_KEY, hash_password("replacement-password-123"))
    await invalid(
        instance,
        "totp",
        {"preauth": pre["preauth"], "code": totp_code(_SEED, clock.wall // 30), "client_ip": _IP},
    )


def change_data(clock, **overrides):
    return {
        "current_password": _PASSWORD,
        "new_password": "changed-password-123",
        "code": totp_code(_SEED, clock.wall // 30),
        "client_ip": _IP,
        **overrides,
    }


async def test_password_change_rotates_credentials_and_allows_new_login(service):
    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    old_credentials = instance.credentials()
    await invalid(instance, "password-change", change_data(clock, current_password="wrong"))
    assert await instance.handle("password-change", change_data(clock)) == {"ok": True}
    assert instance.credentials() != old_credentials
    assert _IP not in instance._ip_failures
    stored = load_env_secrets(instance.store)[PASSWORD_KEY]
    assert verify_password("changed-password-123", stored)
    assert not verify_password(_PASSWORD, stored)
    await invalid(instance, "password", {"password": _PASSWORD, "client_ip": _IP})
    pre = await instance.handle("password", {"password": "changed-password-123", "client_ip": _IP})
    clock.wall += 30
    assert await instance.handle(
        "totp",
        {"preauth": pre["preauth"], "code": totp_code(_SEED, clock.wall // 30), "client_ip": _IP},
    ) == {"ok": True}


@pytest.mark.parametrize("overrides", [{"current_password": "wrong"}, {"code": "abcdef"}])
async def test_password_change_invalid_credentials_lock(service, overrides):
    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    original = instance.credentials()
    for count in range(1, 6):
        await invalid(instance, "password-change", change_data(clock, **overrides))
        assert len(instance._ip_failures[_IP]) == count
    await invalid(instance, "password-change", change_data(clock), "locked")
    assert instance.credentials() == original


async def test_password_change_requires_ip_and_enrolled_totp(service):
    instance, clock = service
    await invalid(instance, "password-change", change_data(clock), "totp_required")
    set_secret(instance.store, TOTP_KEY, _SEED)
    await invalid(instance, "password-change", change_data(clock, client_ip=""))
    pre = await password_login(service)
    data = change_data(clock, preauth=pre["preauth"])
    data.pop("client_ip")
    await invalid(instance, "password-change", data)


@pytest.mark.parametrize("source", ["login", "password-change", "setup"])
async def test_password_change_code_reuse_contract(service, source):
    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    data = change_data(clock)
    if source == "login":
        pre = await password_login(service)
        await instance.handle(
            "totp", {"preauth": pre["preauth"], "code": data["code"], "client_ip": _IP}
        )
    elif source == "password-change":
        await instance.handle("password-change", data)
        data["current_password"] = data["new_password"]
    else:
        instance._verify_code(_SEED, data["code"], setup=True)
    await invalid(
        instance, "password-change", data, "code_reused" if source == "setup" else "invalid"
    )
    assert len(instance._ip_failures.get(_IP, [])) == (0 if source == "setup" else 1)


async def test_password_change_short_password_checks_credentials_first(service):
    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    original = instance.credentials()
    await invalid(
        instance,
        "password-change",
        change_data(clock, current_password="wrong", new_password="short"),
    )
    await invalid(
        instance, "password-change", change_data(clock, code="abcdef", new_password="short")
    )
    await invalid(
        instance, "password-change", change_data(clock, new_password="short"), "password_too_short"
    )
    assert len(instance._ip_failures[_IP]) == 2
    assert instance.credentials() == original
    clock.wall += 30
    assert await instance.handle("password-change", change_data(clock, new_password="x" * 12)) == {
        "ok": True
    }


async def test_password_change_serializes_replayed_code(service):
    import asyncio

    instance, clock = service
    set_secret(instance.store, TOTP_KEY, _SEED)
    results = await asyncio.gather(
        instance.handle("password-change", change_data(clock, new_password=_PASSWORD)),
        instance.handle("password-change", change_data(clock, new_password=_PASSWORD)),
        return_exceptions=True,
    )
    assert sum(result == {"ok": True} for result in results) == 1
    assert sum(isinstance(result, AuthError) for result in results) == 1
