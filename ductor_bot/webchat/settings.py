"""Operational webchat validation, with no filesystem or network effects."""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig

_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_BIND_NETWORKS = tuple(
    ipaddress.IPv4Network(cidr)
    for cidr in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_PORT_FIELDS = (
    "http_port",
    "https_port",
    "npm_admin_port",
    "web_port",
    "bridge_port",
    "crowdsec_lapi_port",
    "crowdsec_appsec_port",
)


def resolve_default_chat_id(config: AgentConfig) -> int:
    """Resolve an explicit chat or the first allowed user, else zero."""
    if config.webchat.default_chat_id:
        return config.webchat.default_chat_id
    return config.allowed_user_ids[0] if config.allowed_user_ids else 0


def _valid_networks(values: list[str]) -> bool:
    try:
        for value in values:
            ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False
    return True


def _valid_bind_address(value: str) -> bool:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return any(address in network for network in _BIND_NETWORKS)


def validate_webchat(config: AgentConfig) -> list[str]:  # noqa: C901, PLR0912
    """Return stable error codes; invalid settings disable webchat only."""
    webchat = config.webchat
    if not webchat.enabled:
        return []
    errors: list[str] = []
    if not config.chat_log.enabled:
        errors.append("chat_log_required")
    if "telegram" not in (config.transports or [config.transport]):
        errors.append("telegram_required")
    if not _DOMAIN.fullmatch(webchat.domain):
        errors.append("domain_invalid")
    if not _valid_networks(webchat.allowed_ips):
        errors.append("allowed_ips_invalid")
    if not _valid_networks(webchat.trusted_proxies):
        errors.append("trusted_proxies_invalid")
    if webchat.exposure == "direct":
        if not webchat.allowed_ips:
            errors.append("allowed_ips_required")
        if not webchat.acme_email:
            errors.append("acme_email_required")
        elif "@" not in webchat.acme_email:
            errors.append("acme_email_invalid")
    else:
        if not webchat.trusted_proxies:
            errors.append("trusted_proxies_required")
        if not _valid_bind_address(webchat.bind_address):
            errors.append("bind_address_invalid")
    errors.extend(
        f"{field}_invalid" for field in _PORT_FIELDS if not 1 <= getattr(webchat, field) <= 65535
    )
    if webchat.npm_admin_port in (webchat.http_port, webchat.https_port):
        errors.append("npm_admin_port_conflict")
    if webchat.bridge_port in (
        config.api.port,
        config.webhooks.port,
        config.interagent_port,
        webchat.web_port,
    ):
        errors.append("bridge_port_conflict")
    if webchat.crowdsec_appsec_port == webchat.crowdsec_lapi_port:
        errors.append("crowdsec_appsec_port_conflict")
    if not resolve_default_chat_id(config):
        errors.append("default_chat_required")
    return errors


def webchat_active(config: AgentConfig, agent_name: str) -> bool:
    """Enable webchat only for the main agent with valid settings."""
    return config.webchat.enabled and agent_name == "main" and not validate_webchat(config)
