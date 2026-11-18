"""Encrypted web credentials and the complete preauth secret lifetime."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ductor_bot.infra.secret_store import set_secret
from ductor_bot.infra.secret_vault import SecretVault
from ductor_bot.webchat import auth, credentials
from ductor_bot.webchat.auth import (
    PASSWORD_KEY,
    TOTP_KEY,
    AuthError,
    BridgeAuth,
    hash_password,
    totp_code,
    verify_password,
)
from ductor_bot.webchat.credentials import (
    CredentialsError,
    WebCredentials,
    decrypt_seed,
)

PASSWORD = "sample-password-123"
NEW_PASSWORD = "replacement-password-123"
SEED = base64.b32encode(b"12345678901234567890").decode()
IP = "198.51.100.7"
Service = tuple[BridgeAuth, SimpleNamespace]


def assert_discarded(context: auth._Preauth) -> None:
    assert context.seed is None
    assert context.seed_key is None
    assert context.seed_salt is None


@pytest.fixture
async def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Service]:
    clock = SimpleNamespace(wall=1_800_000_000, mono=1000.0)
    monkeypatch.setattr(
        auth, "time", SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono)
    )
    instance = BridgeAuth(
        tmp_path / "secrets.env", "example.com", tmp_path / "webchat/auth_state.json"
    )
    instance.credential_store.save(WebCredentials(hash_password(PASSWORD), None))
    yield instance, clock
    await instance.close()


async def password_login(instance: BridgeAuth, ip: str = IP) -> dict[str, object]:
    return await instance.handle("password", {"password": PASSWORD, "client_ip": ip})


async def enroll(service: Service) -> None:
    instance, clock = service
    first = await password_login(instance)
    await instance.handle(
        "setup/begin", {"preauth": first["preauth"], "mode": "paste", "secret": SEED}
    )
    await instance.handle(
        "setup/confirm",
        {"preauth": first["preauth"], "code": totp_code(SEED, clock.wall // 30), "client_ip": IP},
    )
    clock.wall += 30


async def test_registration_encrypts_seed_with_preauth_key_and_no_password_context(
    service: Service,
) -> None:
    instance, clock = service
    first = await password_login(instance)
    token = str(first["preauth"])
    context = instance._preauth[token]
    assert token not in repr(instance._preauth_timers[token])
    assert context.seed_key is not None
    assert len(context.seed_key) == 32
    assert not hasattr(context, "password")
    assert PASSWORD not in repr(context)
    salt = context.seed_salt
    setup = await instance.handle("setup/begin", {"preauth": token, "mode": "new"})
    seed = str(setup["secret"])
    assert await instance.handle(
        "setup/confirm",
        {"preauth": token, "code": totp_code(seed, clock.wall // 30), "client_ip": IP},
    ) == {"ok": True}
    assert_discarded(context)
    assert token not in instance._preauth
    assert not instance._preauth_timers
    saved = instance.credential_store.load()
    assert saved is not None
    assert saved.totp is not None
    assert base64.b64decode(saved.totp.salt) == salt
    assert decrypt_seed(saved.totp, PASSWORD)[0] == seed
    raw = instance.credential_store.path.read_text()
    assert seed not in raw
    assert PASSWORD not in raw
    assert instance.credential_store.path.stat().st_mode & 0o777 == 0o600
    assert not instance.store.exists()
    assert set(json.loads(raw)) == {"password_hash", "totp"}
    assert set(json.loads(raw)["totp"]) == {"salt", "nonce", "ciphertext"}
    assert seed.encode() not in instance.credentials()


async def test_bad_otp_can_retry_and_success_discards_material(service: Service) -> None:
    instance, clock = service
    await enroll(service)
    first = await password_login(instance)
    token = str(first["preauth"])
    context = instance._preauth[token]
    assert context.seed == SEED
    with pytest.raises(AuthError):
        await instance.handle("totp", {"preauth": token, "code": "bad", "client_ip": IP})
    assert instance._preauth[token] is context
    assert context.seed_key is not None
    assert await instance.handle(
        "totp", {"preauth": token, "code": totp_code(SEED, clock.wall // 30), "client_ip": IP}
    ) == {"ok": True}
    assert_discarded(context)
    assert token not in instance._preauth


async def test_reuse_and_restart_high_water_mark(service: Service) -> None:
    instance, clock = service
    await enroll(service)
    first = await password_login(instance)
    code = totp_code(SEED, clock.wall // 30)
    await instance.handle("totp", {"preauth": first["preauth"], "code": code, "client_ip": IP})
    restarted = BridgeAuth(instance.store, instance.domain, instance.state_path)
    try:
        first = await password_login(restarted)
        with pytest.raises(AuthError):
            await restarted.handle(
                "totp", {"preauth": first["preauth"], "code": code, "client_ip": IP}
            )
        clock.wall += 30
        assert await restarted.handle(
            "totp",
            {
                "preauth": first["preauth"],
                "code": totp_code(SEED, clock.wall // 30),
                "client_ip": IP,
            },
        ) == {"ok": True}
    finally:
        await restarted.close()


async def test_expiry_without_requests_discards_material(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _ = service
    await enroll(service)
    monkeypatch.setattr(auth, "_PREAUTH_SECONDS", 0.02)
    first = await password_login(instance)
    token = str(first["preauth"])
    context = instance._preauth[token]
    assert context.seed_key is not None
    await asyncio.sleep(0.05)
    assert token not in instance._preauth
    assert_discarded(context)


async def test_expired_request_and_logout_discard_only_their_contexts(service: Service) -> None:
    instance, clock = service
    await enroll(service)
    first = await password_login(instance)
    token = str(first["preauth"])
    old = instance._preauth[token]
    clock.mono += 301
    with pytest.raises(AuthError):
        await instance.handle("totp", {"preauth": token, "code": "000000", "client_ip": IP})
    assert old.seed_key is None
    first = await password_login(instance)
    second = await password_login(instance, "198.51.100.8")
    old = instance._preauth[str(first["preauth"])]
    await instance.handle("logout", {"preauth": first["preauth"]})
    assert old.seed is None
    assert old.seed_key is None
    assert str(second["preauth"]) in instance._preauth
    await instance.close()
    assert not instance._preauth
    assert not instance._preauth_timers


async def test_ip_limit_discards_secret_context(service: Service) -> None:
    instance, _ = service
    await enroll(service)
    first = await password_login(instance)
    token = str(first["preauth"])
    context = instance._preauth[token]
    for _ in range(5):
        with pytest.raises(AuthError):
            await instance.handle("totp", {"preauth": token, "code": "bad", "client_ip": IP})
    assert context.seed is None
    assert context.seed_key is None
    assert token not in instance._preauth
    with pytest.raises(AuthError, match="locked"):
        await password_login(instance)


async def test_external_credential_change_revokes_and_wipes_preauth(service: Service) -> None:
    instance, _ = service
    await enroll(service)
    first = await password_login(instance)
    context = instance._preauth[str(first["preauth"])]
    saved = instance.credential_store.load()
    assert saved is not None
    instance.credential_store.save(WebCredentials(hash_password(NEW_PASSWORD), saved.totp))
    with pytest.raises(AuthError):
        await instance.handle(
            "totp", {"preauth": first["preauth"], "code": "000000", "client_ip": IP}
        )
    assert context.seed is None
    assert context.seed_key is None


async def test_password_change_reencrypts_seed_and_preserves_reuse_state(service: Service) -> None:
    instance, clock = service
    await enroll(service)
    first = await password_login(instance)
    context = instance._preauth[str(first["preauth"])]
    before = instance.credentials()
    old = instance.credential_store.load()
    assert old is not None
    await instance.handle(
        "password-change",
        {
            "current_password": PASSWORD,
            "new_password": NEW_PASSWORD,
            "code": totp_code(SEED, clock.wall // 30),
            "client_ip": IP,
        },
    )
    saved = instance.credential_store.load()
    assert saved is not None
    assert saved.totp is not None
    assert old.totp is not None
    assert saved.totp.salt != old.totp.salt
    assert verify_password(NEW_PASSWORD, saved.password_hash)
    assert decrypt_seed(saved.totp, NEW_PASSWORD)[0] == SEED
    with pytest.raises(CredentialsError):
        decrypt_seed(saved.totp, PASSWORD)
    assert instance.credentials() != before
    assert context.seed_key is None
    with pytest.raises(AuthError):
        await password_login(instance)
    first = await instance.handle("password", {"password": NEW_PASSWORD, "client_ip": IP})
    with pytest.raises(AuthError):
        await instance.handle(
            "totp",
            {
                "preauth": first["preauth"],
                "code": totp_code(SEED, clock.wall // 30),
                "client_ip": IP,
            },
        )
    clock.wall += 30
    assert await instance.handle(
        "totp",
        {"preauth": first["preauth"], "code": totp_code(SEED, clock.wall // 30), "client_ip": IP},
    ) == {"ok": True}
    raw = instance.credential_store.path.read_text()
    assert SEED not in raw
    assert PASSWORD not in raw
    assert NEW_PASSWORD not in raw


async def test_failed_atomic_password_change_preserves_old_credentials(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, clock = service
    await enroll(service)
    before = instance.credential_store.path.read_bytes()
    writer = Mock(side_effect=OSError("write failed"))
    monkeypatch.setattr(credentials, "atomic_text_save", writer)
    with pytest.raises(AuthError):
        await instance.handle(
            "password-change",
            {
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
                "code": totp_code(SEED, clock.wall // 30),
                "client_ip": IP,
            },
        )
    assert instance.credential_store.path.read_bytes() == before
    payload = writer.call_args.args[1]
    assert SEED not in payload
    assert PASSWORD not in payload
    assert NEW_PASSWORD not in payload
    assert not list(instance.state_path.parent.glob("*.tmp"))


async def test_new_store_login_is_independent_of_locked_vault(service: Service) -> None:
    instance, clock = service
    await enroll(service)
    set_secret(instance.store, "TOOL_TOKEN", "sample-tool-token")
    vault = SecretVault(instance.store)
    await vault.setup("sample-master-key")
    await vault.lock()
    assert not instance.store.exists()
    first = await password_login(instance)
    assert first["next"] == "totp"
    assert await instance.handle(
        "totp",
        {"preauth": first["preauth"], "code": totp_code(SEED, clock.wall // 30), "client_ip": IP},
    ) == {"ok": True}


async def test_seed_and_password_stay_out_of_logs(
    service: Service, caplog: pytest.LogCaptureFixture
) -> None:
    instance, _ = service
    caplog.set_level(logging.DEBUG)
    await enroll(service)
    first = await password_login(instance)
    with pytest.raises(AuthError) as error:
        await instance.handle("totp", {"preauth": first["preauth"], "code": "bad", "client_ip": IP})
    assert PASSWORD not in caplog.text
    assert SEED not in caplog.text
    assert SEED not in str(error.value)
    assert SEED.encode() not in instance.credentials()


async def test_corrupt_new_store_never_falls_back_to_legacy(service: Service) -> None:
    instance, _ = service
    set_secret(instance.store, PASSWORD_KEY, hash_password(PASSWORD))
    set_secret(instance.store, TOTP_KEY, SEED)
    instance.credential_store.path.write_text("{broken")
    with pytest.raises(AuthError):
        await password_login(instance)


async def test_setup_seed_cannot_be_used_for_login_before_confirm(service: Service) -> None:
    instance, clock = service
    first = await password_login(instance)
    await instance.handle(
        "setup/begin", {"preauth": first["preauth"], "mode": "paste", "secret": SEED}
    )
    with pytest.raises(AuthError):
        await instance.handle(
            "totp",
            {
                "preauth": first["preauth"],
                "code": totp_code(SEED, clock.wall // 30),
                "client_ip": IP,
            },
        )
    saved = instance.credential_store.load()
    assert saved is not None
    assert saved.totp is None


@pytest.mark.parametrize("part", ["salt", "nonce", "ciphertext"])
async def test_tampered_seed_ciphertext_is_rejected(service: Service, part: str) -> None:
    instance, _ = service
    await enroll(service)
    data = json.loads(instance.credential_store.path.read_bytes())
    raw = bytearray(base64.b64decode(data["totp"][part]))
    raw[0] ^= 1
    data["totp"][part] = base64.b64encode(raw).decode()
    instance.credential_store.path.write_text(json.dumps(data))
    with pytest.raises(AuthError):
        await password_login(instance)
    assert not instance._preauth


async def test_legacy_generation_does_not_return_plaintext_seed(tmp_path: Path) -> None:
    store = tmp_path / "secrets.env"
    set_secret(store, PASSWORD_KEY, hash_password(PASSWORD))
    set_secret(store, TOTP_KEY, SEED)
    instance = BridgeAuth(store, "example.com", tmp_path / "webchat/auth_state.json")
    before = instance.credentials()
    assert SEED.encode() not in before
    assert json.loads(before)[1] is None
    previous_mtime = store.stat().st_mtime_ns
    set_secret(store, TOTP_KEY, "JBSWY3DPEHPK3PXP")
    os.utime(store, ns=(previous_mtime + 1_000_000_000, previous_mtime + 1_000_000_000))
    assert instance.credentials() != before
    assert not instance.credential_store.path.exists()
