"""Legacy login migration, CLI administration, and encrypted-vault setup ordering."""

from __future__ import annotations

import asyncio
import base64
import getpass
import hashlib
import json
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ductor_bot.infra.env_secrets import load_env_secrets
from ductor_bot.infra.secret_store import set_secret
from ductor_bot.infra.secret_vault import SecretVault, VaultError, VaultState
from ductor_bot.webchat import auth, cli, credentials
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
    CredentialStore,
    WebCredentials,
    decrypt_seed,
    derive_seed_key,
    encrypt_seed,
)
from ductor_bot.workspace.paths import DuctorPaths

PASSWORD = "sample-password-123"
NEW_PASSWORD = "replacement-password-123"
SEED = base64.b32encode(b"12345678901234567890").decode()
IP = "198.51.100.7"
Service = tuple[BridgeAuth, SimpleNamespace]


@pytest.fixture
async def legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Service]:
    clock = SimpleNamespace(wall=1_800_000_000, mono=1000.0)
    monkeypatch.setattr(
        auth, "time", SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono)
    )
    instance = BridgeAuth(
        tmp_path / "secrets.env", "example.com", tmp_path / "webchat/auth_state.json"
    )
    set_secret(instance.store, PASSWORD_KEY, hash_password(PASSWORD))
    set_secret(instance.store, TOTP_KEY, SEED)
    set_secret(instance.store, "OTHER", "sample-tool-value")
    auth._save_auth_state(
        instance.state_path,
        {
            "seed_fingerprint": hashlib.sha256(SEED.encode()).hexdigest(),
            "last_step": clock.wall // 30 - 2,
            "source": "login",
        },
    )
    yield instance, clock
    await instance.close()


async def login(service: Service) -> None:
    instance, clock = service
    first = await instance.handle("password", {"password": PASSWORD, "client_ip": IP})
    assert await instance.handle(
        "totp",
        {"preauth": first["preauth"], "code": totp_code(SEED, clock.wall // 30), "client_ip": IP},
    ) == {"ok": True}


async def test_migration_only_after_totp_success_and_subsequent_encrypted_login(
    legacy: Service,
) -> None:
    instance, clock = legacy
    first = await instance.handle("password", {"password": PASSWORD, "client_ip": IP})
    assert not instance.credential_store.path.exists()
    assert TOTP_KEY in load_env_secrets(instance.store)
    assert await instance.handle(
        "totp",
        {"preauth": first["preauth"], "code": totp_code(SEED, clock.wall // 30), "client_ip": IP},
    ) == {"ok": True}
    saved = instance.credential_store.load()
    assert saved is not None
    assert saved.totp is not None
    assert decrypt_seed(saved.totp, PASSWORD)[0] == SEED
    assert load_env_secrets(instance.store) == {"OTHER": "sample-tool-value"}
    assert SEED not in instance.credential_store.path.read_text()
    assert PASSWORD not in instance.credential_store.path.read_text()
    clock.wall += 30
    await login(legacy)
    assert not instance._preauth


@pytest.mark.parametrize("stage", ["create", "verify", "delete"])
async def test_migration_failure_preserves_both_keys_and_login_success(
    legacy: Service,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stage: str,
) -> None:
    instance, clock = legacy
    before = instance.store.read_bytes()
    failure = Mock(side_effect=OSError("sensitive-canary"))
    with monkeypatch.context() as scoped:
        if stage == "create":
            scoped.setattr(instance.credential_store, "save", failure)
        elif stage == "verify":
            scoped.setattr(instance.credential_store, "verify", failure)
        else:
            scoped.setattr(credentials, "_delete_secrets_locked", failure)
        await login(legacy)
    assert instance.store.read_bytes() == before
    assert not instance.credential_store.path.exists()
    assert "migration deferred" in caplog.text
    assert SEED not in caplog.text
    assert PASSWORD not in caplog.text
    assert "sensitive-canary" not in caplog.text
    clock.wall += 30
    await login(legacy)
    assert instance.credential_store.path.exists()


async def test_encrypted_legacy_store_is_never_rewritten(
    legacy: Service, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    instance, _ = legacy
    marker = instance.store.with_name(instance.store.name + ".enc")
    marker.write_text("encrypted-format-canary")
    before = instance.store.read_bytes()
    writer = Mock(side_effect=AssertionError("plaintext writer must not run"))
    monkeypatch.setattr(credentials, "_delete_secrets_locked", writer)
    await login(legacy)
    writer.assert_not_called()
    assert instance.store.read_bytes() == before
    assert marker.read_text() == "encrypted-format-canary"
    assert not instance.credential_store.path.exists()
    assert "encrypted legacy store" in caplog.text
    assert SEED not in caplog.text


async def test_migrate_then_setup_lock_still_allows_web_login(legacy: Service) -> None:
    instance, clock = legacy
    vault = SecretVault(instance.store)
    with pytest.raises(VaultError, match="WEB_AUTH_MIGRATION_REQUIRED"):
        await vault.setup("sample-master-key")
    await login(legacy)
    await vault.setup("sample-master-key")
    await vault.lock()
    assert (await vault.status()).state is VaultState.LOCKED
    assert not instance.store.exists()
    clock.wall += 30
    await login(legacy)


@pytest.mark.parametrize(
    "entry", [f"{PASSWORD_KEY}=\n", f"{TOTP_KEY}=sample\n", f"export {PASSWORD_KEY}=sample\n"]
)
async def test_setup_guard_checks_legacy_key_presence(tmp_path: Path, entry: str) -> None:
    path = tmp_path / "secrets.env"
    path.write_text(entry)
    vault = SecretVault(path)
    with pytest.raises(VaultError, match="WEB_AUTH_MIGRATION_REQUIRED"):
        await vault.setup("sample-master-key")
    assert path.read_text() == entry
    assert not vault.encrypted_path.exists()


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DuctorPaths:
    paths = DuctorPaths(ductor_home=tmp_path)
    monkeypatch.setattr(cli, "resolve_paths", lambda: paths)
    monkeypatch.setattr(cli, "store_path", lambda _home: tmp_path / "secrets.env")
    return paths


def seeded_new_store(paths: DuctorPaths) -> CredentialStore:
    store = CredentialStore(paths.webchat_dir / "credentials.json")
    salt = b"0123456789abcdef"
    key = derive_seed_key(PASSWORD, salt)
    store.save(WebCredentials(hash_password(PASSWORD), encrypt_seed(SEED, key, salt)))
    return store


def test_cli_set_password_discards_totp_and_cleans_legacy(
    cli_home: DuctorPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    store = seeded_new_store(cli_home)
    legacy = cli_home.ductor_home / "secrets.env"
    set_secret(legacy, PASSWORD_KEY, hash_password(PASSWORD))
    set_secret(legacy, TOTP_KEY, SEED)
    set_secret(legacy, "OTHER", "sample-tool-value")
    with patch.object(getpass, "getpass", side_effect=[NEW_PASSWORD, NEW_PASSWORD]):
        cli.cmd_webchat(["set-password"])
    saved = store.load()
    assert saved is not None
    assert verify_password(NEW_PASSWORD, saved.password_hash)
    assert saved.totp is None
    assert load_env_secrets(legacy) == {"OTHER": "sample-tool-value"}
    assert store.path.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert "TOTP" in output
    assert "required" in output
    assert NEW_PASSWORD not in output
    assert SEED not in output


def test_cli_reset_needs_no_password_and_clear_removes_both_locations(
    cli_home: DuctorPaths,
) -> None:
    store = seeded_new_store(cli_home)
    before = store.load()
    assert before is not None
    path = cli_home.ductor_home / "secrets.env"
    set_secret(path, PASSWORD_KEY, before.password_hash)
    set_secret(path, TOTP_KEY, SEED)
    set_secret(path, "OTHER", "sample-tool-value")
    with patch.object(getpass, "getpass", side_effect=AssertionError("no password needed")):
        cli.cmd_webchat(["reset-totp"])
    saved = store.load()
    assert saved is not None
    assert saved.password_hash == before.password_hash
    assert saved.totp is None
    assert TOTP_KEY not in load_env_secrets(path)
    cli.cmd_webchat(["clear-credentials"])
    assert not store.path.exists()
    assert load_env_secrets(path) == {"OTHER": "sample-tool-value"}


def test_cli_failure_rolls_back_new_store(
    cli_home: DuctorPaths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = seeded_new_store(cli_home)
    before = store.path.read_bytes()
    monkeypatch.setattr(
        cli, "_delete_secrets_locked", Mock(side_effect=OSError("sensitive-canary"))
    )
    with (
        patch.object(getpass, "getpass", side_effect=[NEW_PASSWORD, NEW_PASSWORD]),
        pytest.raises(SystemExit),
    ):
        cli.cmd_webchat(["set-password"])
    assert store.path.read_bytes() == before
    output = capsys.readouterr().out
    assert "CREDENTIALS_UNAVAILABLE" in output
    assert "sensitive-canary" not in output
    assert NEW_PASSWORD not in output


@pytest.mark.parametrize("command", ["set-password", "reset-totp", "clear-credentials"])
async def test_cli_edits_credentials_while_tool_vault_is_locked(
    cli_home: DuctorPaths, command: str
) -> None:
    store = seeded_new_store(cli_home)
    before = store.load()
    assert before is not None
    legacy = cli_home.ductor_home / "secrets.env"
    set_secret(legacy, "OTHER", "synthetic-tool-value")
    vault = SecretVault(legacy)
    await vault.setup("synthetic-master-key")
    await vault.lock()
    encrypted = vault.encrypted_path.read_bytes()
    state = store.path.with_name("auth_state.json")
    cli.rotate_credential_nonce(state)
    nonce = json.loads(state.read_text())["credential_nonce"]
    with patch.object(getpass, "getpass", side_effect=[NEW_PASSWORD, NEW_PASSWORD]):
        cli.cmd_webchat([command])
    saved = store.load()
    if command == "clear-credentials":
        assert saved is None
    else:
        assert saved is not None
        assert saved.totp is None
        assert (
            verify_password(NEW_PASSWORD, saved.password_hash)
            if command == "set-password"
            else saved.password_hash == before.password_hash
        )
    assert json.loads(state.read_text())["credential_nonce"] != nonce
    assert vault.encrypted_path.read_bytes() == encrypted
    assert not legacy.exists()
    assert (await vault.status()).state is VaultState.LOCKED


def test_check_config_reports_storage_and_corruption(
    cli_home: DuctorPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.cmd_webchat(["check-config", "--json"])
    assert json.loads(capsys.readouterr().out)["credentials"] == {
        "storage": "unconfigured",
        "password_configured": False,
        "totp_configured": False,
        "migration_required": False,
    }
    store = seeded_new_store(cli_home)
    cli.cmd_webchat(["check-config", "--json"])
    body = json.loads(capsys.readouterr().out)
    assert body["credentials"]["storage"] == "encrypted"
    assert body["credentials"]["totp_configured"]
    assert SEED not in json.dumps(body)
    store.path.write_text("{broken")
    with pytest.raises(SystemExit):
        cli.cmd_webchat(["check-config", "--json"])
    assert "credentials_invalid" in json.loads(capsys.readouterr().out)["errors"]


def test_check_config_reports_exported_legacy_keys(
    cli_home: DuctorPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    path = cli_home.ductor_home / "secrets.env"
    path.write_text('export DT_WEBCHAT_PASSWORD_HASH=""\n')
    cli.cmd_webchat(["check-config", "--json"])
    report = json.loads(capsys.readouterr().out)["credentials"]
    assert report["storage"] == "legacy"
    assert report["migration_required"]
    assert not report["password_configured"]


@pytest.mark.parametrize("encrypted", [False, True])
async def test_cli_reset_during_password_worker_rejects_old_generation(
    cli_home: DuctorPaths, monkeypatch: pytest.MonkeyPatch, encrypted: bool
) -> None:
    path = cli_home.ductor_home / "secrets.env"
    store = CredentialStore(cli_home.webchat_dir / "credentials.json")
    if encrypted:
        store = seeded_new_store(cli_home)
    else:
        set_secret(path, PASSWORD_KEY, hash_password(PASSWORD))
        set_secret(path, TOTP_KEY, SEED)
    instance = BridgeAuth(path, "example.com", cli_home.webchat_dir / "auth_state.json")
    entered, release = threading.Event(), threading.Event()
    verify = auth.verify_password

    def blocked(password, encoded):
        entered.set()
        assert release.wait(5)
        return verify(password, encoded)

    monkeypatch.setattr(auth, "verify_password", blocked)
    request = asyncio.create_task(
        instance.handle("password", {"password": PASSWORD, "client_ip": IP})
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await asyncio.to_thread(
            cli._edit_credentials, "set-password", store, path, hash_password(NEW_PASSWORD)
        )
        release.set()
        with pytest.raises(AuthError):
            await request
        assert not instance._preauth
        assert not instance._preauth_timers
        saved = store.load()
        assert saved is not None
        assert verify(NEW_PASSWORD, saved.password_hash)
        assert saved.totp is None
    finally:
        release.set()
        await instance.close()


@pytest.mark.parametrize("encrypted", [False, True])
async def test_cli_reset_before_enrollment_commit_rejects_stale_preauth(
    cli_home: DuctorPaths, monkeypatch: pytest.MonkeyPatch, encrypted: bool
) -> None:
    path = cli_home.ductor_home / "secrets.env"
    store = CredentialStore(cli_home.webchat_dir / "credentials.json")
    if encrypted:
        store.save(WebCredentials(hash_password(PASSWORD), None))
    else:
        set_secret(path, PASSWORD_KEY, hash_password(PASSWORD))
    instance = BridgeAuth(path, "example.com", cli_home.webchat_dir / "auth_state.json")
    try:
        first = await instance.handle("password", {"password": PASSWORD, "client_ip": IP})
        token = str(first["preauth"])
        pre = instance._preauth[token]
        await instance.handle("setup/begin", {"preauth": token, "mode": "paste", "secret": SEED})
        complete = auth.complete_write

        async def reset_before_commit(func, *args, **kwargs):
            await asyncio.to_thread(
                cli._edit_credentials, "set-password", store, path, hash_password(NEW_PASSWORD)
            )
            return await complete(func, *args, **kwargs)

        monkeypatch.setattr(auth, "complete_write", reset_before_commit)
        with pytest.raises(AuthError):
            await instance.handle(
                "setup/confirm",
                {
                    "preauth": token,
                    "code": totp_code(SEED, int(auth.time.time() // 30)),
                    "client_ip": IP,
                },
            )
        saved = store.load()
        assert saved is not None
        assert verify_password(NEW_PASSWORD, saved.password_hash)
        assert saved.totp is None
        assert token not in instance._preauth
        assert pre.seed is pre.seed_key is pre.seed_salt is None
    finally:
        await instance.close()


@pytest.mark.parametrize("stage", ["encrypt", "create", "verify", "delete"])
async def test_first_enrollment_failure_never_writes_plaintext_and_can_retry(
    legacy: Service, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    instance, clock = legacy
    from ductor_bot.infra.secret_store import delete_secret

    delete_secret(instance.store, TOTP_KEY)
    before = instance.store.read_bytes()
    first = await instance.handle("password", {"password": PASSWORD, "client_ip": IP})
    token = str(first["preauth"])
    await instance.handle("setup/begin", {"preauth": token, "mode": "paste", "secret": SEED})
    failure = Mock(
        side_effect=credentials.CredentialsError()
        if stage == "encrypt"
        else OSError("synthetic-sensitive-canary")
    )
    with monkeypatch.context() as scoped:
        if stage == "encrypt":
            scoped.setattr(auth, "encrypt_seed", failure)
        elif stage == "create":
            scoped.setattr(instance.credential_store, "save", failure)
        elif stage == "verify":
            scoped.setattr(instance.credential_store, "verify", failure)
        else:
            scoped.setattr(credentials, "_delete_secrets_locked", failure)
        with pytest.raises(AuthError, match="credentials_unavailable"):
            await instance.handle(
                "setup/confirm",
                {"preauth": token, "code": totp_code(SEED, clock.wall // 30), "client_ip": IP},
            )
    assert instance.store.read_bytes() == before
    assert TOTP_KEY not in load_env_secrets(instance.store)
    assert not instance.credential_store.path.exists()
    assert await instance.handle(
        "setup/confirm",
        {"preauth": token, "code": totp_code(SEED, clock.wall // 30), "client_ip": IP},
    ) == {"ok": True}
    saved = instance.credential_store.load()
    assert saved is not None
    assert saved.totp is not None
    assert decrypt_seed(saved.totp, PASSWORD)[0] == SEED
    assert SEED not in instance.credential_store.path.read_text()
