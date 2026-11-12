"""Configuration contracts for independent recording and optional webchat."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ductor_bot.chat_log.settings import chat_log_active, validate_chat_log
from ductor_bot.config import AgentConfig, ChatLogConfig, WebchatConfig, deep_merge_config
from ductor_bot.webchat.settings import (
    resolve_default_chat_id,
    validate_webchat,
    webchat_active,
)
from ductor_bot.workspace.paths import DuctorPaths


@pytest.fixture
def config() -> AgentConfig:
    return AgentConfig(
        chat_log=ChatLogConfig(enabled=True),
        webchat=WebchatConfig(
            enabled=True,
            domain="chat.example.com",
            allowed_ips=["203.0.113.0/24"],
            acme_email="admin@example.com",
        ),
        allowed_user_ids=[123],
    )


def test_defaults_and_disabled_validation(tmp_path: Path) -> None:
    config = AgentConfig()
    assert config.chat_log.model_dump() == {
        "enabled": False,
        "retention_days": 0,
        "backup_retention_days": 0,
    }
    assert config.webchat.model_dump() == {
        "enabled": False,
        "exposure": "direct",
        "domain": "",
        "allowed_ips": [],
        "trusted_proxies": [],
        "bind_address": "127.0.0.1",
        "http_port": 80,
        "https_port": 443,
        "npm_admin_port": 81,
        "acme_email": "",
        "web_port": 8744,
        "bridge_port": 8743,
        "crowdsec_lapi_port": 18080,
        "crowdsec_appsec_port": 17422,
        "default_chat_id": 0,
    }
    assert validate_chat_log(config) == []
    assert validate_webchat(config) == []
    assert not chat_log_active(config, "main")
    assert not webchat_active(config, "main")
    paths = DuctorPaths(ductor_home=tmp_path)
    assert paths.chat_log_dir == tmp_path / "chat_log"
    assert paths.webchat_dir == tmp_path / "webchat"
    assert not paths.chat_log_dir.exists()
    assert not paths.webchat_dir.exists()


def test_new_defaults_merge_without_overwriting_user_settings() -> None:
    merged, changed = deep_merge_config(
        {"chat_log": {"enabled": True}, "webchat": {"domain": "chat.example.com"}},
        AgentConfig().model_dump(),
    )
    config = AgentConfig.model_validate(merged)
    assert changed
    assert config.chat_log.enabled
    assert not config.webchat.enabled
    assert config.webchat.domain == "chat.example.com"


def test_valid_main_and_inherited_subagent(config: AgentConfig) -> None:
    assert validate_chat_log(config) == []
    assert validate_webchat(config) == []
    assert chat_log_active(config, "main")
    assert webchat_active(config, "main")
    assert not chat_log_active(config, "worker")
    assert not webchat_active(config, "worker")


def test_webchat_requires_recording(config: AgentConfig) -> None:
    config.chat_log.enabled = False
    assert validate_webchat(config) == ["chat_log_required"]
    assert not webchat_active(config, "main")


def test_recording_is_independent_of_webchat(config: AgentConfig) -> None:
    config.webchat.enabled = False
    assert chat_log_active(config, "main")
    config.webchat.enabled = True
    config.webchat.domain = "invalid"
    assert not webchat_active(config, "main")
    assert chat_log_active(config, "main")


@pytest.mark.parametrize("transports", [["matrix"], ["matrix", "telegram"]])
def test_telegram_transport_requirement(config: AgentConfig, transports: list[str]) -> None:
    config.transports = transports
    expected = [] if "telegram" in transports else ["telegram_required"]
    assert validate_chat_log(config) == expected
    assert validate_webchat(config) == expected


def test_legacy_transport_fallback(config: AgentConfig) -> None:
    config.transports = []
    config.transport = "matrix"
    assert validate_chat_log(config) == ["telegram_required"]
    assert validate_webchat(config) == ["telegram_required"]
    config.transport = "telegram"
    assert validate_chat_log(config) == []
    assert validate_webchat(config) == []


def test_direct_requires_allowlist_and_email(config: AgentConfig) -> None:
    config.webchat.allowed_ips = []
    config.webchat.acme_email = ""
    assert validate_webchat(config) == ["allowed_ips_required", "acme_email_required"]
    config.webchat.allowed_ips = ["198.51.100.7"]
    config.webchat.acme_email = "invalid"
    assert validate_webchat(config) == ["acme_email_invalid"]


@pytest.mark.parametrize("field", ["allowed_ips", "trusted_proxies"])
def test_network_validation(config: AgentConfig, field: str) -> None:
    setattr(config.webchat, field, ["invalid"])
    assert validate_webchat(config) == [f"{field}_invalid"]
    setattr(config.webchat, field, ["203.0.113.7/24", "2001:db8::/32"])
    assert validate_webchat(config) == []


def test_proxy_requires_trusted_proxies(config: AgentConfig) -> None:
    config.webchat.exposure = "behind_proxy"
    config.webchat.allowed_ips = []
    config.webchat.acme_email = ""
    assert validate_webchat(config) == ["trusted_proxies_required"]
    config.webchat.trusted_proxies = ["198.51.100.7"]
    assert validate_webchat(config) == []


@pytest.mark.parametrize(
    ("address", "valid"),
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("10.0.0.1", True),
        ("172.16.0.1", True),
        ("192.168.0.1", True),
        ("0.0.0.0", False),  # noqa: S104 - reject an unspecified bind address
        ("198.51.100.7", False),
        ("169.254.0.1", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("localhost", False),
    ],
)
def test_proxy_bind_address(config: AgentConfig, address: str, valid: bool) -> None:
    config.webchat.exposure = "behind_proxy"
    config.webchat.trusted_proxies = ["198.51.100.7"]
    config.webchat.bind_address = address
    assert validate_webchat(config) == ([] if valid else ["bind_address_invalid"])


@pytest.mark.parametrize(
    "domain",
    [
        "",
        "localhost",
        "https://chat.example.com",
        "-chat.example.com",
        "chat.example.1",
        "a" * 64 + ".com",
    ],
)
def test_invalid_domain(config: AgentConfig, domain: str) -> None:
    config.webchat.domain = domain
    assert validate_webchat(config) == ["domain_invalid"]


def test_domain_normalization_and_exposure_literal() -> None:
    assert WebchatConfig(domain="CHAT.EXAMPLE.COM").domain == "chat.example.com"
    with pytest.raises(ValidationError, match="exposure"):
        WebchatConfig.model_validate({"exposure": "invalid"})


@pytest.mark.parametrize(
    "field",
    [
        "http_port",
        "https_port",
        "npm_admin_port",
        "web_port",
        "bridge_port",
        "crowdsec_lapi_port",
        "crowdsec_appsec_port",
    ],
)
@pytest.mark.parametrize("port", [0, 65536])
def test_port_ranges_do_not_block_config_loading(
    config: AgentConfig, field: str, port: int
) -> None:
    data = config.model_dump()
    data["webchat"][field] = port
    loaded = AgentConfig.model_validate(data)
    assert validate_webchat(loaded) == [f"{field}_invalid"]
    assert not webchat_active(loaded, "main")


@pytest.mark.parametrize("target", ["api", "webhooks", "interagent", "web"])
def test_bridge_port_conflicts(config: AgentConfig, target: str) -> None:
    ports = {
        "api": config.api.port,
        "webhooks": config.webhooks.port,
        "interagent": config.interagent_port,
        "web": config.webchat.web_port,
    }
    config.webchat.bridge_port = ports[target]
    assert validate_webchat(config) == ["bridge_port_conflict"]


@pytest.mark.parametrize("field", ["http_port", "https_port"])
def test_admin_port_conflicts(config: AgentConfig, field: str) -> None:
    config.webchat.npm_admin_port = getattr(config.webchat, field)
    assert validate_webchat(config) == ["npm_admin_port_conflict"]


def test_crowdsec_port_conflict(config: AgentConfig) -> None:
    config.webchat.crowdsec_appsec_port = config.webchat.crowdsec_lapi_port
    assert validate_webchat(config) == ["crowdsec_appsec_port_conflict"]


def test_default_chat_resolution(config: AgentConfig) -> None:
    assert resolve_default_chat_id(config) == 123
    config.webchat.default_chat_id = -123
    assert resolve_default_chat_id(config) == -123
    config.allowed_user_ids = []
    assert validate_webchat(config) == []
    config.webchat.default_chat_id = 0
    assert resolve_default_chat_id(config) == 0
    assert validate_webchat(config) == ["default_chat_required"]


@pytest.mark.parametrize("field", ["retention_days", "backup_retention_days"])
@pytest.mark.parametrize("value", [-1, 1.5, True, "7"])
def test_retention_rejects_invalid_days(field, value) -> None:
    with pytest.raises(ValidationError, match=field):
        ChatLogConfig.model_validate({field: value})


@pytest.mark.parametrize("value", [None, 0, 7])
def test_retention_accepts_disabled_and_positive_days(value) -> None:
    config = ChatLogConfig(retention_days=value, backup_retention_days=value)
    assert config.retention_days == config.backup_retention_days == value
