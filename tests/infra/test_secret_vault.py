"""Encrypted vault lifetimes, migration safety, authenticated format, and serialization."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ductor_bot.infra import atomic_io, secret_vault
from ductor_bot.infra.secret_vault import SecretVault, VaultError, VaultState

MASTER = "sample-master-key"
NEW_MASTER = "replacement-master-key"
VALUE = "sample-private-value"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def vault(tmp_path: Path) -> AsyncIterator[SecretVault]:
    instance = SecretVault(tmp_path / "secrets.env")
    yield instance
    await instance.lock()


async def test_unconfigured_keeps_plaintext_access(vault: SecretVault) -> None:
    vault.plaintext_path.write_text('# comment\nTOKEN="first"\nALIAS=$TOKEN\nEMPTY=\n')
    assert (await vault.status()).state is VaultState.UNCONFIGURED
    assert await vault.get("ALIAS") == "first"
    assert await vault.list_keys() == ["ALIAS", "EMPTY", "TOKEN"]
    with pytest.raises(VaultError, match="SECRET_NOT_SET"):
        await vault.get("EMPTY")
    await vault.set("TOKEN", VALUE)
    assert await vault.get("TOKEN") == VALUE
    assert await vault.delete("TOKEN")
    assert not await vault.delete("TOKEN")
    assert not vault.encrypted_path.exists()
    await vault.lock()
    assert (await vault.status()).state is VaultState.UNCONFIGURED


async def test_redaction_spans_masks_original_normalized_and_json_escaped_forms(
    vault: SecretVault,
    caplog: pytest.LogCaptureFixture,
) -> None:
    controlled = "ab\x01cd"
    escaped = json.dumps(controlled)[1:-1]
    vault.plaintext_path.write_text(f"TOKEN={controlled}\nOTHER={VALUE}\nEMPTY=\n")
    with caplog.at_level("DEBUG"):
        result = await vault.redaction_spans(
            f"cmd {controlled} {escaped} abcd {VALUE}", lambda text: text.replace("\x01", "")
        )
    normalized = f"cmd abcd {escaped} abcd {VALUE}"
    expected = [(i, i + len(form)) for i, form in (
        (4, "abcd"), (9, escaped), (10 + len(escaped), "abcd"), (15 + len(escaped), VALUE),
    )]
    assert result is not None
    assert sorted(result) == expected
    assert [normalized[start:end] for start, end in sorted(result)] == ["abcd", escaped, "abcd", VALUE]
    for form in (controlled, escaped, "abcd", VALUE):
        assert form not in caplog.text


async def test_redaction_spans_masks_both_json_encodings_of_non_ascii_value(vault: SecretVault) -> None:
    value = f'synthetic-{chr(233)}-"quoted"-value'
    vault.plaintext_path.write_text(f"TOKEN={value}\n", encoding="utf-8")
    ascii_json = json.dumps(value)[1:-1]
    unicode_json = json.dumps(value, ensure_ascii=False)[1:-1]
    result = await vault.redaction_spans(f"cmd {ascii_json} {unicode_json}", str)
    assert result is not None
    assert sorted(result) == [(4, 4 + len(ascii_json)), (5 + len(ascii_json), 5 + len(ascii_json) + len(unicode_json))]


async def test_redaction_spans_includes_overlapping_occurrences(vault: SecretVault) -> None:
    vault.plaintext_path.write_text("VALUE=aaaa\nOTHER=aaaaaa\n")
    result = await vault.redaction_spans("aaaaaa", str)
    assert result is not None
    assert sorted(result) == [(0, 4), (0, 6), (1, 5), (2, 6)]


async def test_redaction_spans_suppresses_short_forms_before_and_after_normalization(
    vault: SecretVault,
) -> None:
    vault.plaintext_path.write_text("SHORT=ab\nNORMALIZED=x\x01yz\n")

    def normalize(text: str) -> str:
        return text.replace("\x01", "")

    assert await vault.redaction_spans("cmd ab", normalize) is None
    assert await vault.redaction_spans("cmd x\x01yz", normalize) is None
    assert await vault.redaction_spans("cmd x\x01\x01yz", normalize) is None
    assert await vault.redaction_spans("cmd other", normalize) == []


async def test_redaction_spans_io_error_contains_no_values(
    vault: SecretVault,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault.plaintext_path.write_text(f"TOKEN={VALUE}\n")
    monkeypatch.setattr(Path, "read_bytes", Mock(side_effect=OSError(VALUE)))
    with caplog.at_level("DEBUG"), pytest.raises(VaultError, match=r"^SECRETS_IO_ERROR$") as error:
        await vault.redaction_spans(f"cmd {VALUE}", str)
    assert VALUE not in str(error.value)
    assert VALUE not in caplog.text


async def test_redaction_spans_expired_store_raises_locked(tmp_path: Path) -> None:
    clock = Clock()
    vault = SecretVault(tmp_path / "secrets.env", ttl_seconds=1, clock=clock)
    vault.plaintext_path.write_text(f"TOKEN={VALUE}\n")
    await vault.setup(MASTER)
    assert await vault.redaction_spans(f"cmd {VALUE}", str) == [(4, 4 + len(VALUE))]
    clock.now += 1
    with pytest.raises(VaultError, match=r"^SECRETS_LOCKED$"):
        await vault.redaction_spans(f"cmd {VALUE}", str)


async def test_setup_preserves_text_authenticates_header_and_restarts_locked(
    vault: SecretVault,
) -> None:
    text = '# comment\r\nTOKEN="sample-private-value"\r\nALIAS=$TOKEN\r\n'
    vault.plaintext_path.write_bytes(text.encode())
    await vault.setup(MASTER)
    assert not vault.plaintext_path.exists()
    assert vault.encrypted_path.stat().st_mode & 0o777 == 0o600
    envelope = json.loads(vault.encrypted_path.read_bytes())
    header = envelope["header"]
    assert set(header) == {"version", "kdf", "salt", "nonce"}
    assert header["kdf"] == {"name": "scrypt", "n": 32768, "r": 8, "p": 1}
    salt = base64.b64decode(header["salt"])
    nonce = base64.b64decode(header["nonce"])
    assert len(salt) == 16
    assert len(nonce) == 12
    key = secret_vault._derive(MASTER, salt)
    aad = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    assert (
        AESGCM(key).decrypt(nonce, base64.b64decode(envelope["ciphertext"]), aad) == text.encode()
    )
    assert MASTER.encode() not in vault.encrypted_path.read_bytes()
    assert VALUE.encode() not in vault.encrypted_path.read_bytes()
    assert await vault.get("ALIAS") == VALUE
    restarted = SecretVault(vault.plaintext_path)
    assert (await restarted.status()).state is VaultState.LOCKED
    with pytest.raises(VaultError, match="SECRETS_LOCKED"):
        await restarted.get("TOKEN")
    await restarted.unlock(MASTER)
    assert await restarted.get("TOKEN") == VALUE
    await restarted.lock()
    with pytest.raises(VaultError, match="MASTER_KEY_ALREADY_SET"):
        await vault.setup(MASTER)


async def test_setup_empty_store(vault: SecretVault) -> None:
    await vault.setup(MASTER)
    assert await vault.list_keys() == []
    assert (await vault.status()).state is VaultState.UNLOCKED


@pytest.mark.parametrize("master", ["", "short", "1234567", "\ud80012345678"])
async def test_new_key_validation(vault: SecretVault, master: str) -> None:
    vault.plaintext_path.write_text(f"TOKEN={VALUE}\n")
    with pytest.raises(VaultError, match="MASTER_KEY_TOO_SHORT"):
        await vault.setup(master)
    assert vault.plaintext_path.exists()
    assert not vault.encrypted_path.exists()
    await vault.setup(MASTER)
    before = vault.encrypted_path.read_bytes()
    with pytest.raises(VaultError, match="MASTER_KEY_TOO_SHORT"):
        await vault.change_key(MASTER, master)
    assert vault.encrypted_path.read_bytes() == before


async def test_setup_write_failure_preserves_source(
    vault: SecretVault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = f"TOKEN={VALUE}\n"
    vault.plaintext_path.write_text(source)
    monkeypatch.setattr(
        secret_vault, "atomic_bytes_save", Mock(side_effect=OSError("write failed"))
    )
    with pytest.raises(VaultError, match="SECRETS_IO_ERROR"):
        await vault.setup(MASTER)
    assert vault.plaintext_path.read_text() == source
    assert (await vault.status()).state is VaultState.UNCONFIGURED
    assert not vault.encrypted_path.exists()


async def test_setup_unlink_failure_rolls_back_ciphertext(
    vault: SecretVault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = f"TOKEN={VALUE}\n"
    vault.plaintext_path.write_text(source)
    unlink = Path.unlink

    def fail_source(path: Path, *, missing_ok: bool = False) -> None:
        if path == vault.plaintext_path:
            raise PermissionError
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_source)
    with pytest.raises(VaultError, match="SECRETS_IO_ERROR"):
        await vault.setup(MASTER)
    assert vault.plaintext_path.read_text() == source
    assert not vault.encrypted_path.exists()
    assert (await vault.status()).state is VaultState.UNCONFIGURED


async def test_atomic_temp_contains_ciphertext_and_failure_cleans_it(
    vault: SecretVault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = f"TOKEN={VALUE}\n"
    vault.plaintext_path.write_text(source)
    write = atomic_io._write_all
    observed: list[bytes] = []

    def fail_after_write(fd: int, data: bytes) -> None:
        observed.append(data)
        write(fd, data)
        raise OSError("injected failure")

    monkeypatch.setattr(atomic_io, "_write_all", fail_after_write)
    with pytest.raises(VaultError, match="SECRETS_IO_ERROR"):
        await vault.setup(MASTER)
    assert observed
    assert all(VALUE.encode() not in data and MASTER.encode() not in data for data in observed)
    assert not list(vault.plaintext_path.parent.glob("*.tmp"))
    assert vault.plaintext_path.read_text() == source


async def test_wrong_key_throttle_and_reset(vault: SecretVault) -> None:
    clock = Clock()
    vault._clock = clock
    await vault.setup(MASTER)
    await vault.lock()
    for delay in (1, 2, 4, 8, 16, 32, 60, 60):
        with pytest.raises(VaultError, match="INVALID_MASTER_KEY") as wrong:
            await vault.unlock("bad")
        assert wrong.value.code == "INVALID_MASTER_KEY"
        with pytest.raises(VaultError, match="SECRETS_RATE_LIMITED") as limited:
            await vault.unlock(MASTER)
        assert limited.value.retry_after == delay
        clock.now += delay
    await vault.unlock(MASTER)
    await vault.lock()
    with pytest.raises(VaultError, match="INVALID_MASTER_KEY"):
        await vault.unlock("\ud800")
    with pytest.raises(VaultError, match="SECRETS_RATE_LIMITED") as reset:
        await vault.unlock(MASTER)
    assert reset.value.retry_after == 1
    assert (await vault.status()).state is VaultState.LOCKED


async def test_reveal_reauth_shared_throttle_and_no_refresh(vault: SecretVault) -> None:
    clock = Clock()
    vault._clock = clock
    await vault.setup(MASTER)
    await vault.set("TOKEN", VALUE)
    await vault.set_ttl(100)
    clock.now += 20
    with pytest.raises(VaultError, match="INVALID_MASTER_KEY"):
        await vault.reveal("TOKEN", "wrong-key")
    with pytest.raises(VaultError, match="SECRETS_RATE_LIMITED"):
        await vault.change_key(MASTER, NEW_MASTER)
    with pytest.raises(VaultError, match="SECRETS_RATE_LIMITED"):
        await vault.unlock(MASTER)
    clock.now += 1
    assert await vault.reveal("TOKEN", MASTER) == VALUE
    assert (await vault.status()).remaining_seconds == 79
    with pytest.raises(VaultError, match="SECRET_NOT_SET"):
        await vault.reveal("MISSING", MASTER)
    await vault.lock()
    with pytest.raises(VaultError, match="SECRETS_LOCKED"):
        await vault.reveal("TOKEN", MASTER)


async def test_change_key_keeps_absolute_deadline_and_uses_fresh_salt(vault: SecretVault) -> None:
    clock = Clock()
    vault._clock = clock
    await vault.setup(MASTER)
    await vault.set("TOKEN", VALUE)
    await vault.set_ttl(100)
    before = json.loads(vault.encrypted_path.read_bytes())["header"]
    clock.now += 30
    await vault.change_key(MASTER, NEW_MASTER)
    after = json.loads(vault.encrypted_path.read_bytes())["header"]
    assert before["salt"] != after["salt"]
    assert before["nonce"] != after["nonce"]
    assert (await vault.status()).remaining_seconds == 70
    assert await vault.get("TOKEN") == VALUE
    await vault.lock()
    with pytest.raises(VaultError, match="INVALID_MASTER_KEY"):
        await vault.unlock(MASTER)
    clock.now += 1
    await vault.unlock(NEW_MASTER)
    assert await vault.get("TOKEN") == VALUE


async def test_ttl_changes_recalculate_from_unlock_and_unlimited(vault: SecretVault) -> None:
    clock = Clock()
    vault._clock = clock
    await vault.setup(MASTER)
    await vault.set_ttl(100)
    clock.now += 30
    await vault.set_ttl(80)
    assert (await vault.status()).remaining_seconds == 50
    await vault.set_ttl(0, MASTER)
    clock.now += 1000
    assert (await vault.status()).state is VaultState.UNLOCKED
    assert (await vault.status()).remaining_seconds is None
    assert vault._expiry_task is None
    await vault.set_ttl(10)
    assert (await vault.status()).state is VaultState.LOCKED
    await vault.set_ttl(0, MASTER)
    assert (await vault.status()).state is VaultState.LOCKED
    await vault.unlock(MASTER)
    assert (await vault.status()).state is VaultState.UNLOCKED


async def test_expiry_timer_drops_material_without_status_polling(vault: SecretVault) -> None:
    await vault.setup(MASTER)
    await vault.set("TOKEN", VALUE)
    await vault.set_ttl(1)
    await asyncio.sleep(1.1)
    assert vault._key is None
    assert vault._salt is None
    assert vault._text is None
    assert not vault._values
    assert vault._unlocked_at is None
    assert vault._expiry_task is None


@pytest.mark.parametrize("ttl", [-1, True, 1.2])
async def test_invalid_ttl(vault: SecretVault, ttl: int) -> None:
    with pytest.raises(VaultError, match="INVALID_TTL"):
        await vault.set_ttl(ttl)


@pytest.mark.parametrize("part", ["ciphertext", "salt", "nonce"])
async def test_authenticated_tampering_is_rejected(vault: SecretVault, part: str) -> None:
    await vault.setup(MASTER)
    await vault.lock()
    envelope = json.loads(vault.encrypted_path.read_bytes())
    container = envelope if part == "ciphertext" else envelope["header"]
    data = bytearray(base64.b64decode(container[part]))
    data[0] ^= 1
    container[part] = base64.b64encode(data).decode()
    vault.encrypted_path.write_text(json.dumps(envelope))
    with pytest.raises(VaultError, match="INVALID_MASTER_KEY"):
        await vault.unlock(MASTER)
    assert (await vault.status()).state is VaultState.LOCKED


@pytest.mark.parametrize("field", ["version", "kdf", "salt", "nonce", "ciphertext"])
async def test_invalid_envelope_rejected_before_kdf(
    vault: SecretVault,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    await vault.setup(MASTER)
    await vault.lock()
    envelope = json.loads(vault.encrypted_path.read_bytes())
    if field == "ciphertext":
        envelope[field] = "%%%"
    elif field == "kdf":
        envelope["header"][field]["n"] = 2**31
    else:
        envelope["header"][field] = True
    vault.encrypted_path.write_text(json.dumps(envelope))
    derive = Mock(side_effect=AssertionError("KDF must not run"))
    monkeypatch.setattr(secret_vault, "_derive", derive)
    with pytest.raises(VaultError, match="INVALID_SECRETS_FILE"):
        await vault.unlock(MASTER)
    derive.assert_not_called()


async def test_mutations_references_and_concurrent_writes(vault: SecretVault) -> None:
    vault.plaintext_path.write_text(f"# comment\nTOKEN={VALUE}\nTOKEN=last\nALIAS=$TOKEN\n")
    await vault.setup(MASTER)
    await asyncio.gather(*(vault.set(f"KEY_{i}", f"value-{i}") for i in range(12)))
    assert len(await vault.list_keys()) == 14
    assert await vault.get("TOKEN") == "last"
    await vault.set("TOKEN", "updated")
    assert await vault.get("ALIAS") == "updated"
    assert await vault.delete("TOKEN")
    assert not await vault.delete("TOKEN")
    await vault.lock()
    await vault.unlock(MASTER)
    for i in range(12):
        assert await vault.get(f"KEY_{i}") == f"value-{i}"
    assert not list(vault.plaintext_path.parent.glob("*.tmp"))


@pytest.mark.parametrize("operation", ["set", "delete", "change_key"])
async def test_failed_save_retains_previous_memory_and_ciphertext(
    vault: SecretVault,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    await vault.setup(MASTER)
    await vault.set("TOKEN", VALUE)
    before = vault.encrypted_path.read_bytes()
    monkeypatch.setattr(secret_vault, "atomic_bytes_save", Mock(side_effect=OSError("failed")))
    pending: Awaitable[object]
    if operation == "set":
        pending = vault.set("TOKEN", "replacement")
    elif operation == "delete":
        pending = vault.delete("TOKEN")
    else:
        pending = vault.change_key(MASTER, NEW_MASTER)
    with pytest.raises(VaultError, match="SECRETS_IO_ERROR"):
        await pending
    assert await vault.get("TOKEN") == VALUE
    assert vault.encrypted_path.read_bytes() == before
    assert (await vault.status()).state is VaultState.UNLOCKED


async def test_cancelled_save_finishes_before_lock_and_leaves_no_stale_memory(
    vault: SecretVault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await vault.setup(MASTER)
    await vault.set("TOKEN", VALUE)
    started, release = threading.Event(), threading.Event()
    save = atomic_io.atomic_bytes_save

    def delayed_save(path: Path, content: bytes) -> None:
        started.set()
        assert release.wait(5)
        save(path, content)

    monkeypatch.setattr(secret_vault, "atomic_bytes_save", delayed_save)
    writer = asyncio.create_task(vault.set("TOKEN", "updated"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        writer.cancel()
        waiter = asyncio.create_task(vault.lock())
        await asyncio.sleep(0)
        assert not waiter.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await writer
    await waiter
    assert (await vault.status()).state is VaultState.LOCKED
    monkeypatch.setattr(secret_vault, "atomic_bytes_save", save)
    await vault.unlock(MASTER)
    assert await vault.get("TOKEN") == "updated"


async def test_locked_errors_and_diagnostics_do_not_include_values(
    vault: SecretVault,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await vault.setup(MASTER)
    await vault.set("TOKEN", VALUE)
    await vault.lock()
    for operation in (
        vault.get("TOKEN"),
        vault.list_keys(),
        vault.set("TOKEN", VALUE),
        vault.delete("TOKEN"),
    ):
        with pytest.raises(VaultError, match="SECRETS_LOCKED") as error:
            await operation
        assert MASTER not in str(error.value)
        assert VALUE not in repr(error.value)
    assert MASTER not in repr(await vault.status())
    assert VALUE not in repr(await vault.status())
    assert MASTER not in caplog.text
    assert VALUE not in caplog.text


async def test_ciphertext_only_reaches_atomic_writer(
    vault: SecretVault,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault.plaintext_path.write_text(f"TOKEN={VALUE}\n")
    writer = atomic_io.atomic_bytes_save
    writes: list[bytes] = []

    def inspect_write(path: Path, content: bytes) -> None:
        assert MASTER.encode() not in content
        assert NEW_MASTER.encode() not in content
        assert VALUE.encode() not in content
        assert path == vault.encrypted_path
        writes.append(content)
        writer(path, content)

    monkeypatch.setattr(secret_vault, "atomic_bytes_save", inspect_write)
    await vault.setup(MASTER)
    await vault.set("ANOTHER", VALUE)
    await vault.delete("ANOTHER")
    await vault.change_key(MASTER, NEW_MASTER)
    assert len(writes) == 4
    assert not vault.plaintext_path.exists()
    assert not list(vault.plaintext_path.parent.glob("*.tmp"))


async def test_invalid_secret_encoding_is_value_free(vault: SecretVault) -> None:
    await vault.setup(MASTER)
    before = vault.encrypted_path.read_bytes()
    with pytest.raises(VaultError, match="INVALID_SECRET_VALUE") as error:
        await vault.set("TOKEN", "private\ud800suffix")
    assert "private" not in str(error.value)
    assert "suffix" not in str(error.value)
    assert await vault.list_keys() == []
    assert vault.encrypted_path.read_bytes() == before


@pytest.mark.parametrize("placement", ["file", "dangling"])
async def test_setup_rejects_symlink_paths_without_touching_source(tmp_path: Path, placement):
    original = tmp_path / "original/nested/secrets.env"
    original.parent.mkdir(parents=True)
    original.write_text("TOKEN=synthetic-symlink-value\n")
    before = original.read_bytes(), original.stat().st_mtime_ns
    link = tmp_path / "linked"
    link.symlink_to(original if placement == "file" else tmp_path / "missing.env")
    path = link
    vault = SecretVault(path)
    with pytest.raises(VaultError, match=r"^SECRETS_FILE_SYMLINK$"):
        await vault.setup(MASTER)
    assert (await vault.status()).state is VaultState.UNCONFIGURED
    assert original.read_bytes() == before[0]
    assert original.stat().st_mtime_ns == before[1]
    assert link.is_symlink()
    assert not vault.encrypted_path.exists()
    assert not path.with_name(path.name + ".lock").exists()
    assert vault._key is None
    assert vault._text is None


async def test_regular_file_setup_still_removes_plaintext_and_unlocks(tmp_path: Path):
    path = tmp_path / "regular/secrets.env"
    path.parent.mkdir()
    path.write_text("TOKEN=synthetic-regular-value\n")
    vault = SecretVault(path)
    await vault.setup(MASTER)
    assert not path.exists()
    assert vault.encrypted_path.is_file()
    assert b"synthetic-regular-value" not in vault.encrypted_path.read_bytes()
    await vault.lock()
    await vault.unlock(MASTER)
    assert await vault.get("TOKEN") == "synthetic-regular-value"
    await vault.lock()


@pytest.mark.parametrize("placement", ["parent", "ancestor"])
async def test_setup_allows_parent_symlinks_without_leaving_plaintext(tmp_path: Path, placement):
    original = tmp_path / "original/nested/secrets.env"
    original.parent.mkdir(parents=True)
    original.write_text("TOKEN=synthetic-parent-link-value\n")
    link = tmp_path / "linked"
    if placement == "parent":
        link.symlink_to(original.parent, target_is_directory=True)
        path = link / "secrets.env"
    else:
        link.symlink_to(original.parent.parent, target_is_directory=True)
        path = link / "nested/secrets.env"
    vault = SecretVault(path)
    await vault.setup(MASTER)
    assert link.is_symlink()
    assert not path.exists()
    assert not original.exists()
    assert vault.encrypted_path.resolve().is_file()
    assert b"synthetic-parent-link-value" not in vault.encrypted_path.read_bytes()
    await vault.lock()
    restarted = SecretVault(path)
    assert (await restarted.status()).state is VaultState.LOCKED
    await restarted.unlock(MASTER)
    assert await restarted.get("TOKEN") == "synthetic-parent-link-value"
    await restarted.lock()


async def test_ttl_extension_requires_key_and_shares_backoff(vault: SecretVault):
    clock = Clock()
    vault._clock = clock
    await vault.setup(MASTER)
    await vault.set_ttl(60)
    unlocked_at = vault._unlocked_at
    for longer in (120, 0):
        with pytest.raises(VaultError, match="MASTER_KEY_REQUIRED"):
            await vault.set_ttl(longer)
    assert vault._failures == 0
    assert (await vault.status()).ttl_seconds == 60
    with pytest.raises(VaultError, match="INVALID_MASTER_KEY"):
        await vault.set_ttl(120, "synthetic-wrong-key")
    assert (await vault.status()).ttl_seconds == 60
    with pytest.raises(VaultError, match="SECRETS_RATE_LIMITED"):
        await vault.reveal("TOKEN", MASTER)
    clock.now += 2
    await vault.set_ttl(120, MASTER)
    assert vault._unlocked_at == unlocked_at
    assert (await vault.status()).remaining_seconds == 118
    await vault.set_ttl(30)
    await vault.set_ttl(30)
    await vault.lock()
    await vault.set_ttl(0, MASTER)
    assert (await vault.status()).state is VaultState.LOCKED
    await vault.set_ttl(10)
    assert (await vault.status()).ttl_seconds == 10


async def test_unconfigured_ttl_changes_need_no_key(vault: SecretVault):
    await vault.set_ttl(60)
    await vault.set_ttl(120)
    await vault.set_ttl(0)
    assert (await vault.status()).state is VaultState.UNCONFIGURED
    assert (await vault.status()).ttl_seconds == 0
