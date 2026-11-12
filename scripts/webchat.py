#!/usr/bin/env python3
"""Manage the optional webchat gateway from the Compose project directory.

The bot CLI owns configuration validation. ``check-config --json`` must return
{"errors": [error codes], "chat_log": {"enabled": bool}, "webchat": {all
WebchatConfig fields}} without secrets. A failed check exits 1 before writes.
After rerunning onboarding with --force, rerun this script's up command to
restore the generated DTW_* values and COMPOSE_PROFILES in the project .env.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import http.cookiejar
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
HEALTH_TIMEOUT_SECONDS = 30
PROXY_HOST_PENDING_MARKER = "# dtw-certificate-pending"
SERVICES = ("dt-webchat-web", "dt-webchat-npm", "dt-webchat-crowdsec")
VOLUMES = ("dtw-npmplus-data", "dtw-crowdsec-data", "dtw-crowdsec-config")
NPMPLUS_COMMON_TEMPLATE_SOURCE_SHA256 = (
    "a97ffe67ff769df42fec6be915bf0dc0476492e57a33189b9b54388fec55c8e7"
)
# Extracted from the pinned NPMplus image, /usr/local/nginx/conf/nginx.conf.
NPMPLUS_NGINX_SOURCE_SHA256 = "95d4bcb9857691c749dd91256dab17dde38826f46c74ba600fcd51984118df1a"
DISABLED_SCENARIOS = (
    "crowdsecurity/http-generic-bf",
    "crowdsecurity/appsec-native",
    "crowdsecurity/crowdsec-appsec-outofband",
)


class GatewayError(RuntimeError):
    """A sanitized operational failure; never include subprocess output."""


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)  # noqa: PTH105 - explicit atomic-write contract
    finally:
        with contextlib.suppress(FileNotFoundError):
            Path(temporary).unlink()


def env_values(content: str) -> dict[str, str]:
    values = {}
    for line in content.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*?)\s*$", line)
        if match:
            raw = match[2]
            if raw.startswith(("'", '"')) and raw.endswith(raw[0]):
                raw = raw[1:-1]
            else:
                raw = re.split(r"\s+#", raw, maxsplit=1)[0]
            values[match[1]] = raw
    return values


def update_env(content: str, updates: dict[str, str], *, enable: bool) -> str:
    """Preserve unrelated lines and merge only the webchat profile."""
    updates = dict(updates)
    profiles = env_values(content).get("COMPOSE_PROFILES", "").split(",")
    profiles = list(dict.fromkeys(p.strip() for p in profiles if p.strip() != "webchat"))
    profiles = [p for p in profiles if p]
    if enable:
        profiles.append("webchat")
    updates["COMPOSE_PROFILES"] = ",".join(profiles)
    result = []
    seen = set()
    for line in content.splitlines(keepends=True):
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=", line)
        if match and match[1] in updates:
            key = match[1]
            if key not in seen:
                result.append(f"{key}={json.dumps(updates[key])}\n")
                seen.add(key)
        else:
            result.append(line)
    if result and not result[-1].endswith("\n"):
        result[-1] += "\n"
    result.extend(
        f"{key}={json.dumps(value)}\n" for key, value in updates.items() if key not in seen
    )
    return "".join(result)


def compose_settings(config: dict[str, Any]) -> dict[str, str]:
    proxy = config["exposure"] == "behind_proxy"
    origin = f"https://{config['domain']}"
    if not proxy and config["https_port"] != 443:
        origin += f":{config['https_port']}"
    return {
        "DTW_PUBLIC_ORIGIN": origin,
        "DTW_DOMAIN": config["domain"],
        "DTW_WEB_PORT": str(config["web_port"]),
        "DTW_BRIDGE_PORT": str(config["bridge_port"]),
        "DTW_NPM_ADMIN_PORT": str(config["npm_admin_port"]),
        "DTW_NPM_IPV4_BINDING": config["bind_address"] if proxy else "0.0.0.0",  # noqa: S104 - direct listener
        "DTW_NPM_IPV6_BINDING": "[::]",
        "DTW_NPM_DISABLE_IPV6": str(proxy).lower(),
        "DTW_NPM_HTTP_PORT": str(config["http_port"]),
        "DTW_NPM_HTTPS_PORT": str(config["https_port"]),
        "DTW_ACME_EMAIL": config["acme_email"],
        "DTW_CROWDSEC_LAPI_PORT": str(config["crowdsec_lapi_port"]),
        "DTW_CROWDSEC_APPSEC_PORT": str(config["crowdsec_appsec_port"]),
    }


def render_nginx_config(source: str, config: dict[str, Any]) -> str:
    """Limit proxy trust and exempt only the loopback management listener from AppSec."""
    if hashlib.sha256(source.encode()).hexdigest() != NPMPLUS_NGINX_SOURCE_SHA256:
        raise GatewayError("npmplus_nginx_source_mismatch")
    networks = dict.fromkeys(
        str(ipaddress.ip_network(value, strict=False))
        for value in config["trusted_proxies"]
        if config["exposure"] == "behind_proxy"
    )
    trusted = "".join(f"  set_real_ip_from {network};\n" for network in networks)
    stripped = re.sub(r"^  set_real_ip_from [^;]+;\n", "", source, flags=re.MULTILINE)
    stripped = stripped.replace(
        "  real_ip_header X-Forwarded-For;\n", "  real_ip_header X-Forwarded-For;\n" + trusted
    )
    # NPMplus 2026-07-24-r1 nginx.conf:176-178 defines this bouncer variable.
    # Its pinned lua-cs-bouncer checks for "1" before calling AppSec.
    return stripped.replace(
        "  map $host $crowdsec_disable_appsec {\n    default 0;\n  }",
        '  map "$server_addr:$server_port" $crowdsec_disable_appsec {\n'
        "    default 0;\n"
        f'    "127.0.0.1:{config["npm_admin_port"]}" 1;\n'
        f'    "::1:{config["npm_admin_port"]}" 1;\n'
        "  }",
    )


def render_http_top(config: dict[str, Any]) -> str:
    default = int(not config["allowed_ips"] and config["exposure"] == "behind_proxy")
    networks = dict.fromkeys(
        str(ipaddress.ip_network(v, strict=False)) for v in config["allowed_ips"]
    )
    entries = "".join(f"    {network} 1;\n" for network in networks)
    return (
        f"geo $dtw_ip_ok {{\n    default {default};\n{entries}}}\n"
        'map "$dtw_ip_ok:$uri" $dtw_block {\n'
        "    default 1;\n"
        '    "~^1:" 0;\n'
        '    "~^0:/\\.well-known/acme-challenge/" 0;\n'
        "}\n"
    )


def render_crs_after(config: dict[str, Any]) -> str:
    host = re.escape(config["domain"])
    rules = (
        (
            9600100,
            "POST",
            "@rx",
            r"^/api/topics/-?[0-9]+(?:\.[0-9]+)?/messages$",
            "ARGS:json.text",
            True,
        ),
        (
            9600101,
            "PUT",
            "@rx",
            r"^/api/files/(?:PERSONA|BEHAVIOR|MAINMEMORY|SHAREDMEMORY)\.md$",
            "ARGS:json.content",
            True,
        ),
        (9600102, "GET", "@streq", "/api/search", "ARGS:q", False),
        (9600103, "POST", "@streq", "/api/auth/password", "ARGS:json.password", True),
    )
    result = []
    for rule_id, method, operator, path, field, body in rules:
        tags = ["attack-rce", "attack-lfi"] + (["attack-xss", "attack-sqli"] if body else [])
        ctl = ",".join(f"ctl:ruleRemoveTargetByTag={tag};{field}" for tag in tags)
        result.append(
            f'SecRule REQUEST_HEADERS:Host "@rx ^{host}(?::[0-9]+)?$" '
            f'"id:{rule_id},phase:1,pass,nolog,t:none,t:lowercase,chain"\n'
            f'  SecRule REQUEST_METHOD "@streq {method}" "t:none,chain"\n'
            f'    SecRule REQUEST_FILENAME "{operator} {path}" "t:none,{ctl}"\n'
        )
    return "".join(result)


def render_crs_setup() -> str:
    return """SecAction "id:900990,phase:1,pass,t:none,nolog,setvar:tx.crs_setup_version=400"
SecAction "id:900000,phase:1,pass,t:none,nolog,setvar:tx.blocking_paranoia_level=1"
SecAction "id:900001,phase:1,pass,t:none,nolog,setvar:tx.detection_paranoia_level=1"
SecAction "id:900110,phase:1,pass,t:none,nolog,setvar:tx.inbound_anomaly_score_threshold=5,setvar:tx.outbound_anomaly_score_threshold=5"
SecAction "id:900200,phase:1,pass,t:none,nolog,setvar:'tx.allowed_methods=GET HEAD POST OPTIONS PUT PATCH DELETE'"
"""


def render_acquisition() -> str:
    return """filenames:
  - /npmplus-data/nginx/logs/access.log
  - /npmplus-data/nginx/logs/error.log
labels:
  type: npmplus
---
listen_addr: 0.0.0.0:7422
appsec_configs:
  - crowdsecurity/appsec-default
  - custom/dtw-crs
source: appsec
labels:
  type: appsec
"""


def render_appsec() -> str:
    # The three block-mode filters are copied verbatim from npmwaf-gateway.
    return APPSEC_CONFIG


APPSEC_CONFIG = """name: custom/dtw-crs
default_remediation: ban
inband_rules:
  - crowdsecurity/crs
on_match:
  - filter: |
      IsInBand == true &&
      any(evt.Appsec.MatchedRules, #.name == "native_rule:913100") &&
      any([".nasl", "advanced email extractor", "arachni/", "autogetcontent", "bilbo", "bfac", "burpcollaborator", "cgichk", "cisco-torch", "core-project/1.0", "crimscanner/", "datacha0s", "detectify", "dirbuster", "domino hunter", "ecairn-grabber", "email extractor", "fhscan core 1.", "floodgate", "fuzz faster u fool", "f-secure radar", "get-minimal", "gobuster", "grabber", "grendel-scan", "httpx - open-source project", "inspath", "internet ninja", "jaascois", "jorgee", "masscan", "metis", "morfeus fucking scanner", "n-stealth", "nessus", "netsparker", "nmap nse", "nmap scripting engine", "nmap-nse", "nsauditor", "nuclei", "openvas", "paros", "prog.customcrawler", "qualys was", "s.t.a.l.k.e.r.", "security scan", "springenwerk", "toata dragostea", "toata dragostea mea pentru diavola", "user-agent:", "vega/", "voideye", "w3af.sf.net", "w3af.sourceforge.net", "w3af.org", "webbandit", "webinspect", "webshag", "webtrends security analyzer", "webvulnscan", "wfuzz", "whatweb", "whcc/", "zgrab"], { Lower(req.Header.Get("User-Agent")) contains # })
    apply:
      - CancelAlert()
  - filter: |
      IsInBand == true &&
      any(evt.Appsec.MatchedRules, #.name == "native_rule:913100") &&
      !any([".nasl", "advanced email extractor", "arachni/", "autogetcontent", "bilbo", "bfac", "burpcollaborator", "cgichk", "cisco-torch", "core-project/1.0", "crimscanner/", "datacha0s", "detectify", "dirbuster", "domino hunter", "ecairn-grabber", "email extractor", "fhscan core 1.", "floodgate", "fuzz faster u fool", "f-secure radar", "get-minimal", "gobuster", "grabber", "grendel-scan", "httpx - open-source project", "inspath", "internet ninja", "jaascois", "jorgee", "masscan", "metis", "morfeus fucking scanner", "n-stealth", "nessus", "netsparker", "nmap nse", "nmap scripting engine", "nmap-nse", "nsauditor", "nuclei", "openvas", "paros", "prog.customcrawler", "qualys was", "s.t.a.l.k.e.r.", "security scan", "springenwerk", "toata dragostea", "toata dragostea mea pentru diavola", "user-agent:", "vega/", "voideye", "w3af.sf.net", "w3af.sourceforge.net", "w3af.org", "webbandit", "webinspect", "webshag", "webtrends security analyzer", "webvulnscan", "wfuzz", "whatweb", "whcc/", "zgrab"], { Lower(req.Header.Get("User-Agent")) contains # })
    apply:
      - SendAlert()
  - filter: |
      IsInBand == true &&
      !any(evt.Appsec.MatchedRules, #.name == "native_rule:913100")
    apply:
      - CancelAlert()
"""


def render_bouncer(config: dict[str, Any], key: str) -> str:
    return (
        f"ENABLED=true\nAPI_KEY={key}\n"
        f"API_URL=http://127.0.0.1:{config['crowdsec_lapi_port']}\n"
        f"APPSEC_URL=http://127.0.0.1:{config['crowdsec_appsec_port']}\n"
        "APPSEC_FAILURE_ACTION=passthrough\nREQUEST_TIMEOUT=10000\n"
        "UPDATE_FREQUENCY=30\nMODE=stream\n"
    )


def proxy_payload(config: dict[str, Any], certificate_id: int = 0) -> dict[str, Any]:
    tls = config["exposure"] == "direct" and certificate_id > 0
    return {
        "domain_names": [config["domain"]],
        "forward_scheme": "http",
        "forward_host": "127.0.0.1",
        "forward_port": config["web_port"],
        "certificate_id": certificate_id if tls else 0,
        "ssl_forced": tls,
        "http2_support": tls,
        "hsts_enabled": tls,
        "hsts_subdomains": False,
        "npmplus_crowdsec_appsec": False,
        "block_exploits": True,
        "caching_enabled": False,
        "allow_websocket_upgrade": True,
        "advanced_config": "if ($dtw_block) { return 403; }",
        "locations": [],
    }


def port_is_listening(port: int) -> bool:
    """Read Linux LISTEN sockets without opening a listener."""
    for table in ("tcp", "tcp6"):
        try:
            rows = Path(f"/proc/net/{table}").read_text().splitlines()[1:]
        except FileNotFoundError as error:
            if table == "tcp6":  # IPv6 can be disabled on the host.
                continue
            raise GatewayError("direct_port_check_failed") from error
        except OSError as error:
            raise GatewayError("direct_port_check_failed") from error
        if any(
            row.split()[3] == "0A" and int(row.split()[1].rsplit(":", 1)[1], 16) == port
            for row in rows
        ):
            return True
    return False


class Gateway:
    def __init__(self, repo: Path = REPO) -> None:
        self.repo = repo
        self.env_path = repo / ".env"
        self.environment = dict(os.environ)
        content = self.env_path.read_text() if self.env_path.exists() else ""
        self.saved_env = env_values(content)
        home = self.environment.get("DUCTOR_HOME") or self.saved_env.get("DUCTOR_HOME", "~/.ductor")
        self.state = Path(home).expanduser() / "webchat" / "gateway"
        self.config: dict[str, Any] = {}
        self.pending_config_hashes: dict[Path, str] = {}
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),  # noqa: S323 - local NPM API contract
        )

    def run(
        self, args: list[str], *, data: str | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            args,
            input=data,
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=self.environment,
            check=False,
        )
        if check and result.returncode:
            raise GatewayError("gateway_command_failed")
        return result

    def compose(
        self, *args: str, data: str | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            ["docker", "compose", "--profile", "webchat", *args], data=data, check=check
        )

    def execute(
        self, service: str, *args: str, data: str | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.compose("exec", "-T", service, *args, data=data, check=check)

    def preflight(self) -> None:
        result = self.compose(
            "run",
            "--rm",
            "--no-deps",
            "ductor-bot",
            "ductor",
            "webchat",
            "check-config",
            "--json",
            check=False,
        )
        try:
            checked = json.loads(result.stdout)
            errors = checked["errors"]
            self.config = checked["webchat"]
            if self.config["enabled"] and not checked["chat_log"]["enabled"]:
                errors = list(dict.fromkeys([*errors, "chat_log_required"]))
            if errors:
                # Error codes only: never echo a malformed CLI response or its stderr.
                codes = [
                    code
                    for code in errors
                    if isinstance(code, str) and re.fullmatch(r"[a-z_]+", code)
                ]
                raise GatewayError(", ".join(codes) or "config_invalid")
            if result.returncode:
                raise GatewayError("check_config_failed")
            if not self.config["enabled"]:
                raise GatewayError("webchat_disabled")
            settings = compose_settings(self.config)
            domain = self.environment.get("DTW_DOMAIN", settings["DTW_DOMAIN"])
            if not domain or domain != self.config["domain"]:
                raise GatewayError("domain_mismatch")
            token = self.state.parent / "share" / "bridge.token"
            try:
                if not token.is_file() or not token.read_text().strip():
                    raise GatewayError("bridge_token_missing: restart ductor-bot before running up")
            except OSError as exc:
                raise GatewayError(
                    "bridge_token_unreadable: check DUCTOR_UID matches the host UID"
                ) from exc
            self.environment.update(settings)
            if self.config["exposure"] == "direct":
                self.check_ports()
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayError("check_config_invalid_json") from exc

    def check_ports(self) -> None:
        container = self.compose("ps", "-q", "dt-webchat-npm").stdout.strip()
        owned = {}
        if container:
            info = json.loads(self.run(["docker", "inspect", container]).stdout)[0]
            if info["State"]["Running"]:
                owned = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
        for key, field in (("HTTP_PORT", "http_port"), ("HTTPS_PORT", "https_port")):
            if port_is_listening(self.config[field]) and (
                owned.get(key) != str(self.config[field]) or owned.get("IPV4_BINDING") != "0.0.0.0"  # noqa: S104 - direct listener
            ):
                raise GatewayError("direct_port_in_use")

    def verify_npmplus_common_template_source(self) -> None:
        definition = json.loads(self.compose("config", "--format", "json").stdout)
        image = definition["services"]["dt-webchat-npm"]["image"]
        actual = self.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sha256sum",
                image,
                "/app/templates/_common.conf",
            ]
        ).stdout.split()
        if not actual or actual[0] != NPMPLUS_COMMON_TEMPLATE_SOURCE_SHA256:
            raise GatewayError("npmplus_template_source_mismatch")

    def verified_nginx_config(self) -> str:
        definition = json.loads(self.compose("config", "--format", "json").stdout)
        image = definition["services"]["dt-webchat-npm"]["image"]
        source = self.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "cat",
                image,
                "/usr/local/nginx/conf/nginx.conf",
            ]
        ).stdout
        return render_nginx_config(source, self.config)

    def initialized(self) -> bool:
        if self.run(["docker", "volume", "inspect", VOLUMES[0]], check=False).returncode:
            return False
        definition = json.loads(self.compose("config", "--format", "json").stdout)
        image = definition["services"]["dt-webchat-npm"]["image"]
        return not self.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/bin/sh",
                "-v",
                f"{VOLUMES[0]}:/data:ro",
                image,
                "-c",
                "test -s /data/database.sqlite",
            ],
            check=False,
        ).returncode

    def wait_healthy(self, service: str, timeout: int = 180) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            container = self.compose("ps", "-q", service).stdout.strip()
            if container:
                status = self.run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                        container,
                    ]
                ).stdout.strip()
                if status == "healthy":
                    return
            time.sleep(2)
        raise GatewayError("gateway_health_timeout")

    def npm_request(
        self, path: str, payload: dict[str, Any] | None = None, *, method: str | None = None
    ) -> Any:
        url = f"https://127.0.0.1:{self.config['npm_admin_port']}/api{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with self.opener.open(request, timeout=180) as response:
                content = response.read()
                return json.loads(content) if content else None
        except (OSError, ValueError) as exc:
            raise GatewayError("npm_api_failed") from exc

    def login(self, credentials: dict[str, str]) -> None:
        for attempt in range(40):
            try:
                self.npm_request(
                    "/tokens",
                    {"identity": credentials["identity"], "secret": credentials["password"]},
                )
                self.npm_request("/users")
            except GatewayError:
                if attempt == 39:
                    raise
                time.sleep(3)
            else:
                return

    def start_services(self) -> None:
        secret = self.state / "npm-admin.secret"
        initialized = self.initialized()
        if secret.exists():
            secret.chmod(0o600)
            credentials = json.loads(secret.read_text())
        elif initialized:
            raise GatewayError("npm_admin_secret_missing")
        else:
            credentials = {
                "identity": "admin@chat.example.com",
                "password": secrets.token_urlsafe(36),
            }
            atomic_write(secret, json.dumps(credentials))
        template = self.repo / "docker/webchat/npmplus/_common.conf"
        template_hash = hashlib.sha256(template.read_bytes()).hexdigest()
        hash_file = self.state / "common-template.sha256"
        nginx_hash = hashlib.sha256((self.state / "nginx.conf").read_bytes()).hexdigest()
        nginx_hash_file = self.state / "nginx-config.sha256"
        changed = (
            not hash_file.exists()
            or hash_file.read_text().strip() != template_hash
            or not nginx_hash_file.exists()
            or nginx_hash_file.read_text().strip() != nginx_hash
        )
        if initialized:
            self.compose("up", "-d", "--build", *SERVICES)
            if changed:
                self.compose("up", "-d", "--no-deps", "--force-recreate", "dt-webchat-npm")
        else:
            override = self.state / "bootstrap.compose.json"
            atomic_write(
                override,
                json.dumps(
                    {
                        "services": {
                            "dt-webchat-npm": {
                                "environment": {
                                    "INITIAL_ADMIN_EMAIL": "${INITIAL_ADMIN_EMAIL:?}",
                                    "INITIAL_ADMIN_PASSWORD": "${INITIAL_ADMIN_PASSWORD:?}",
                                }
                            }
                        }
                    }
                ),
            )
            self.environment.update(
                INITIAL_ADMIN_EMAIL=credentials["identity"],
                INITIAL_ADMIN_PASSWORD=credentials["password"],
            )
            try:
                self.run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(self.repo / "compose.yaml"),
                        "-f",
                        str(override),
                        "--profile",
                        "webchat",
                        "up",
                        "-d",
                        "--build",
                        *SERVICES,
                    ]
                )
            finally:
                self.environment.pop("INITIAL_ADMIN_EMAIL", None)
                self.environment.pop("INITIAL_ADMIN_PASSWORD", None)
                override.unlink()
        for service in SERVICES:
            self.wait_healthy(service)
        self.login(credentials)
        # Plain Compose removes INITIAL_ADMIN_* from the container environment.
        self.compose("up", "-d", "--no-deps", "dt-webchat-npm")
        self.wait_healthy("dt-webchat-npm")
        container = self.compose("ps", "-q", "dt-webchat-npm").stdout.strip()
        env = self.run(["docker", "inspect", "--format", "{{json .Config.Env}}", container]).stdout
        if any(v.startswith("INITIAL_ADMIN_") for v in json.loads(env)):
            raise GatewayError("bootstrap_retirement_failed")
        self.login(credentials)
        self.pending_config_hashes = (
            {hash_file: template_hash, nginx_hash_file: nginx_hash} if changed else {}
        )

    def sync_file(self, service: str, path: str, content: str) -> bool:
        match = self.execute(
            service,
            "sh",
            "-c",
            'test -f "$1" && cmp -s - "$1"',
            "sh",
            path,
            data=content,
            check=False,
        )
        if not match.returncode:
            return False
        self.execute(
            service,
            "sh",
            "-eu",
            "-c",
            """
            umask 077
            mkdir -p "$(dirname "$1")"
            t=$(mktemp "${1}.tmp.XXXXXX")
            trap 'rm -f "$t"' EXIT
            cat >"$t"
            mv -f "$t" "$1"
        """,
            "sh",
            path,
            data=content,
        )
        return True

    def crowdsec(self) -> None:
        service = "dt-webchat-crowdsec"
        collections = self.execute(service, "cscli", "collections", "list", "-o", "raw").stdout
        changed = "crowdsecurity/appsec-crs," not in collections
        if changed:
            self.execute(service, "cscli", "collections", "install", "crowdsecurity/appsec-crs")
        scenarios = self.execute(service, "cscli", "scenarios", "list", "-o", "raw").stdout
        for scenario in DISABLED_SCENARIOS:
            if any(line.split(",")[0] == scenario for line in scenarios.splitlines()):
                self.execute(service, "cscli", "scenarios", "remove", scenario, "--force")
                changed = True
        for path, content in (
            ("/var/lib/crowdsec/data/crs-setup.conf", render_crs_setup()),
            (
                "/var/lib/crowdsec/data/crs-plugins/dtw/dtw-after.conf",
                render_crs_after(self.config),
            ),
            ("/etc/crowdsec/appsec-configs/dtw-crs.yaml", render_appsec()),
            ("/etc/crowdsec/acquis.d/npmplus.yaml", render_acquisition()),
        ):
            changed = self.sync_file(service, path, content) or changed
        bouncers = self.execute(service, "cscli", "bouncers", "list", "-o", "raw").stdout
        exists = any(
            row.get("name") == "dt-webchat-npm" for row in csv.DictReader(io.StringIO(bouncers))
        )
        key_file = self.state / "bouncer.key"
        if exists and not key_file.exists():
            self.execute(service, "cscli", "bouncers", "delete", "dt-webchat-npm")
            exists = False
        if not exists:
            key = self.execute(
                service, "cscli", "bouncers", "add", "dt-webchat-npm", "-o", "raw"
            ).stdout.strip()
            if not key:
                raise GatewayError("bouncer_key_missing")
            atomic_write(key_file, key)
        key_file.chmod(0o600)
        key = key_file.read_text().strip()
        npm_changed = self.sync_file(
            "dt-webchat-npm", "/data/crowdsec/crowdsec.conf", render_bouncer(self.config, key)
        )
        if changed:
            self.compose("kill", "-s", "HUP", service)
            self.wait_healthy(service, 120)
        if npm_changed:
            self.compose("restart", "dt-webchat-npm")
            self.wait_healthy("dt-webchat-npm", 120)
        self.probe_appsec(key)

    def probe_appsec(self, key: str) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.config['crowdsec_appsec_port']}/",
            headers={
                "X-Crowdsec-Appsec-Api-Key": key,
                "X-Crowdsec-Appsec-Ip": "127.0.0.1",
                "X-Crowdsec-Appsec-Uri": "/",
                "X-Crowdsec-Appsec-Host": self.config["domain"],
                "X-Crowdsec-Appsec-Verb": "GET",
            },
        )
        for attempt in range(30):
            try:
                with self.opener.open(request, timeout=5) as response:
                    if response.status == 200:
                        return
            except urllib.error.HTTPError as exc:
                if exc.code == 403:
                    return
            except OSError:
                pass
            if attempt != 29:
                time.sleep(2)
        raise GatewayError("appsec_probe_failed")

    def nginx_config(self) -> None:
        # Keep the previous file until both syntax validation and reload succeed.
        self.execute(
            "dt-webchat-npm", "sh", "-eu", "-c", NGINX_UPDATE, data=render_http_top(self.config)
        )

    def proxy_host(self) -> None:
        hosts = self.npm_request("/nginx/proxy-hosts")
        host = next((h for h in hosts if self.config["domain"] in h["domain_names"]), None)
        if host and PROXY_HOST_PENDING_MARKER in host.get("advanced_config", ""):
            self.npm_request(f"/nginx/proxy-hosts/{host['id']}", method="DELETE")
            host = None
        payload = proxy_payload(self.config, int(host.get("certificate_id", 0)) if host else 0)
        if host:
            host_id = host["id"]
            if self.pending_config_hashes or any(
                host.get(key) != value for key, value in payload.items()
            ):
                self.npm_request(f"/nginx/proxy-hosts/{host_id}", payload, method="PUT")
        else:
            if self.config["exposure"] == "direct":
                payload["advanced_config"] += "\n" + PROXY_HOST_PENDING_MARKER
            host_id = self.npm_request("/nginx/proxy-hosts", payload)["id"]
        if self.config["exposure"] == "direct" and not payload["certificate_id"]:
            try:
                certificates = self.npm_request("/nginx/certificates")
                certificate = next(
                    (
                        c
                        for c in certificates
                        if c["provider"] == "letsencrypt"
                        and self.config["domain"] in c["domain_names"]
                    ),
                    None,
                )
                if certificate is None:
                    certificate = self.npm_request(
                        "/nginx/certificates",
                        {
                            "provider": "letsencrypt",
                            "domain_names": [self.config["domain"]],
                            "meta": {"dns_challenge": False},
                        },
                    )
                self.npm_request(
                    f"/nginx/proxy-hosts/{host_id}",
                    proxy_payload(self.config, int(certificate["id"])),
                    method="PUT",
                )
            except (GatewayError, KeyError, TypeError, ValueError) as exc:
                if host is None:
                    try:
                        self.npm_request(f"/nginx/proxy-hosts/{host_id}", method="DELETE")
                    except GatewayError:
                        error = str(exc) if isinstance(exc, GatewayError) else "certificate_failed"
                        raise GatewayError(f"{error}; proxy_host_cleanup_failed") from exc
                raise

    def health(self) -> None:
        proxy = self.config["exposure"] == "behind_proxy"
        address = self.config["bind_address"] if proxy else "127.0.0.1"
        port = self.config["http_port"] if proxy else self.config["https_port"]
        scheme = "http" if proxy else "https"
        probes = (
            ("web", [], f"http://127.0.0.1:{self.config['web_port']}/api/health"),
            (
                "gateway",
                [
                    "--resolve",
                    f"{self.config['domain']}:{port}:{address}",
                    "--dump-header",
                    "-",
                ],
                f"{scheme}://{self.config['domain']}:{port}/api/health",
            ),
        )
        for name, extra, url in probes:
            # NPM applies certificate updates with an asynchronous nginx reload.
            deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
            status = "unavailable"
            healthy = False
            while (remaining := deadline - time.monotonic()) > 0:
                result = self.run(
                    [
                        "curl",
                        "--noproxy",
                        "*",
                        "--silent",
                        "--show-error",
                        "--max-time",
                        str(min(20, remaining)),
                        *extra,
                        "--output",
                        "/dev/null",
                        "--write-out",
                        "\n%{http_code}",
                        url,
                    ],
                    check=False,
                )
                headers, _, status = result.stdout.rpartition("\n")
                basic_auth = re.search(r"(?im)^WWW-Authenticate\s*:", headers)
                healthy = not result.returncode and (
                    status == "200" or (name == "gateway" and status == "403" and not basic_auth)
                )
                if healthy:
                    break
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(1, remaining))
            # Only the final numeric status is safe to echo, never response data.
            safe_status = status if re.fullmatch(r"[0-9]{3}", status) else "unavailable"
            print(f"{name}_health_status={safe_status}")
            if not healthy:
                raise GatewayError(f"{name}_health_failed")

    def up(self) -> None:
        self.preflight()
        self.verify_npmplus_common_template_source()
        nginx_config = self.verified_nginx_config()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        (self.state.parent / "share").mkdir(exist_ok=True, mode=0o700)
        content = self.env_path.read_text() if self.env_path.exists() else ""
        content = update_env(content, compose_settings(self.config), enable=True)
        values = env_values(content)
        if not values["DTW_DOMAIN"] or values["DTW_DOMAIN"] != self.config["domain"]:
            raise GatewayError("domain_mismatch")
        atomic_write(self.env_path, content)
        atomic_write(self.state / "nginx.conf", nginx_config, mode=0o644)
        self.start_services()
        self.crowdsec()
        self.nginx_config()
        credentials = json.loads((self.state / "npm-admin.secret").read_text())
        self.login(credentials)
        self.proxy_host()
        # Commit only after host generation (including certificate attachment) succeeds.
        for path, digest in self.pending_config_hashes.items():
            atomic_write(path, digest + "\n")
        self.pending_config_hashes.clear()
        self.health()

    def down(self, *, purge: bool = False) -> None:
        self.compose("stop", *SERVICES)
        self.compose("rm", "-f", *SERVICES)
        if self.env_path.exists():
            atomic_write(self.env_path, update_env(self.env_path.read_text(), {}, enable=False))
        if purge:
            present = [
                volume
                for volume in VOLUMES
                if not self.run(["docker", "volume", "inspect", volume], check=False).returncode
            ]
            if present:
                self.run(["docker", "volume", "rm", *present])
            if self.state.exists():
                shutil.rmtree(self.state)


NGINX_UPDATE = """
f=/data/custom_nginx/http_top.conf
mkdir -p /data/custom_nginx
backup=$(mktemp -d)
had_old=0
committed=0
if [ -e "$f" ]; then cp -p "$f" "$backup/previous"; had_old=1; fi
restore() {
    if [ "$had_old" = 1 ]; then
        cp -p "$backup/previous" "${f}.restore"
        mv -f "${f}.restore" "$f"
    else
        rm -f "$f"
    fi
}
finish() {
    status=$?
    trap - EXIT
    if [ "$committed" = 0 ]; then
        restore
        nginx -t && nginx -s reload || true
    fi
    rm -rf "$backup"
    rm -f "${f}.tmp"
    exit "$status"
}
trap finish EXIT
trap 'exit 1' HUP INT TERM
umask 022
cat >"${f}.tmp"
mv -f "${f}.tmp" "$f"
nginx -t
nginx -s reload
committed=1
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "down", "status"))
    parser.add_argument(
        "--purge", action="store_true", help="Remove gateway volumes and state (down only)"
    )
    args = parser.parse_args(argv)
    if args.purge and args.command != "down":
        parser.error("--purge requires down")
    try:
        gateway = Gateway()
        if args.command == "up":
            gateway.up()
        elif args.command == "down":
            gateway.down(purge=args.purge)
        else:
            print(gateway.compose("ps", *SERVICES).stdout, end="")
    except GatewayError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        # Never print third-party exceptions: API responses may contain credentials.
        print("webchat_gateway_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
