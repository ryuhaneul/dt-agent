"""Gateway rendering and orchestration tests; Docker and network are always fake."""


# The scripts CI job uses stdlib unittest without pytest or package installation.
# ruff: noqa: INP001, PT009, PT027

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import ipaddress
import json
import os
import posixpath
import re
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("webchat_gateway", ROOT / "scripts/webchat.py")
GW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GW)


def config(**overrides):
    values = {
        "enabled": True,
        "exposure": "direct",
        "domain": "chat.example.com",
        "allowed_ips": ["203.0.113.7/24", "198.51.100.7"],
        "trusted_proxies": [],
        "bind_address": "127.0.0.1",
        "http_port": 80,
        "https_port": 443,
        "npm_admin_port": 81,
        "acme_email": "admin@example.com",
        "web_port": 8744,
        "bridge_port": 8743,
        "crowdsec_lapi_port": 18080,
        "crowdsec_appsec_port": 17422,
        "default_chat_id": 1,
    }
    values.update(overrides)
    return values


def completed(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, "")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class GatewayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="webchat-gateway-test-")
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / "repo"
        self.repo.mkdir()
        self.home = Path(temp.name) / "home"
        environment = patch.dict(os.environ, {"DUCTOR_HOME": str(self.home)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.gateway = GW.Gateway(self.repo)
        self.gateway.config = config()

    def test_pinned_nginx_trust_is_limited_to_configured_proxies(self):
        source = (ROOT / "scripts/tests/fixtures/npmplus-nginx.conf").read_text()
        self.assertEqual(
            hashlib.sha256(source.encode()).hexdigest(), GW.NPMPLUS_NGINX_SOURCE_SHA256
        )
        for exposure, expected in (("direct", []), ("behind_proxy", ["10.0.0.1/32"])):
            with self.subTest(exposure=exposure):
                rendered = GW.render_nginx_config(
                    source, config(exposure=exposure, trusted_proxies=["10.0.0.1/32"])
                )
                networks = re.findall(r"set_real_ip_from ([^;]+);", rendered)
                self.assertEqual(networks, expected)
                peer = ipaddress.ip_address("10.0.0.2")
                self.assertFalse(any(peer in ipaddress.ip_network(net) for net in networks))
                stripped = re.sub(r"^  set_real_ip_from [^;]+;\n", "", source, flags=re.MULTILINE)
                normalize_map = r"  map [^\n]+ \$crowdsec_disable_appsec \{.*?\n  \}"
                self.assertEqual(
                    re.sub(
                        normalize_map,
                        "",
                        re.sub(r"^  set_real_ip_from [^;]+;\n", "", rendered, flags=re.MULTILINE),
                        flags=re.DOTALL,
                    ),
                    re.sub(normalize_map, "", stripped, flags=re.DOTALL),
                )
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn(":/etc/dtw-nginx.conf:ro", compose)
        self.assertIn('entrypoint: ["/bin/sh", "/etc/dtw-nginx-entrypoint.sh"]', compose)
        self.assertIn(":/etc/dtw-nginx-entrypoint.sh:ro", compose)
        self.assertNotIn("TRUST_IP:", compose)

    def test_appsec_bypass_is_limited_to_loopback_management_listener(self):
        source = (ROOT / "scripts/tests/fixtures/npmplus-nginx.conf").read_text()
        for port in (81, 8181):
            with self.subTest(port=port):
                rendered = GW.render_nginx_config(source, config(npm_admin_port=port))
                block = re.search(
                    r'map "\$server_addr:\$server_port" \$crowdsec_disable_appsec \{(.*?)\}',
                    rendered,
                    re.DOTALL,
                ).group(1)
                entries = dict(re.findall(r'"([^" ]+)" ([01]);', block))
                self.assertIn("default 0;", block)
                self.assertEqual(entries, {f"127.0.0.1:{port}": "1", f"::1:{port}": "1"})
                for address in ("127.0.0.1", "::1", "203.0.113.7"):
                    for public_port in (80, 443):
                        self.assertEqual(entries.get(f"{address}:{public_port}", "0"), "0")
                self.assertNotIn("$host", block)
                self.assertNotIn("$http_", block)
                self.assertFalse(GW.proxy_payload(config())["npmplus_crowdsec_appsec"])

    def test_nginx_source_mismatch_stops_before_writes(self):
        with (
            patch.object(self.gateway, "preflight"),
            patch.object(self.gateway, "verify_npmplus_common_template_source"),
            patch.object(
                self.gateway,
                "compose",
                return_value=completed(
                    json.dumps(
                        {"services": {"dt-webchat-npm": {"image": "example/image@sha256:test"}}}
                    )
                ),
            ),
            patch.object(self.gateway, "run", return_value=completed("wrong nginx source")),
            self.assertRaisesRegex(GW.GatewayError, "npmplus_nginx_source_mismatch"),
        ):
            self.gateway.up()
        self.assertFalse(self.home.exists())
        self.assertFalse((self.repo / ".env").exists())

    def test_nginx_wrapper_copies_policy_before_original_entrypoint_and_fails_closed(self):
        source = self.repo / "policy.conf"
        target = self.repo / "nginx.conf"
        marker = self.repo / "started"
        tini = self.repo / "tini"
        tini.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$DTW_TEST_MARKER"\n')
        tini.chmod(0o755)
        script = (ROOT / "docker/webchat/npmplus/entrypoint.sh").read_text()
        script = script.replace("/etc/dtw-nginx.conf", str(source)).replace(
            "/usr/local/nginx/conf/nginx.conf", str(target)
        )
        environment = {"PATH": f"{self.repo}:/usr/bin:/bin", "DTW_TEST_MARKER": str(marker)}
        result = subprocess.run(
            ["sh", "-c", script], env=environment, capture_output=True, check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        source.write_text("verified policy")
        result = subprocess.run(
            ["sh", "-c", script], env=environment, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(), "verified policy")
        self.assertEqual(marker.read_text().splitlines(), ["--", "entrypoint.sh"])

    def test_web_image_uses_the_bot_uid_build_argument(self):
        compose = (ROOT / "compose.yaml").read_text()
        block = compose.split("  dt-webchat-web:\n", 1)[1].split("  dt-webchat-npm:", 1)[0]
        self.assertIn('DUCTOR_UID: "${DUCTOR_UID:-1000}"', block)
        dockerfile = (ROOT / "docker/webchat/Dockerfile").read_text()
        self.assertIn("ARG DUCTOR_UID=1000", dockerfile)
        self.assertIn('--uid "${DUCTOR_UID}"', dockerfile)

    def test_public_origin_uses_direct_https_port_only(self):
        self.assertEqual(
            GW.compose_settings(config(https_port=8443))["DTW_PUBLIC_ORIGIN"],
            "https://chat.example.com:8443",
        )
        self.assertEqual(
            GW.compose_settings(config())["DTW_PUBLIC_ORIGIN"], "https://chat.example.com"
        )
        self.assertEqual(
            GW.compose_settings(config(exposure="behind_proxy", https_port=8443))[
                "DTW_PUBLIC_ORIGIN"
            ],
            "https://chat.example.com",
        )
        self.assertIn(
            "DTW_PUBLIC_ORIGIN: ${DTW_PUBLIC_ORIGIN:-https://chat.example.com}",
            (ROOT / "compose.yaml").read_text(),
        )

    def test_gateway_services_receive_timezone_without_shared_env_secrets(self):
        compose = (ROOT / "compose.yaml").read_text()
        for service in GW.SERVICES:
            block = re.search(
                rf"(?ms)^  {re.escape(service)}:\n(.*?)(?=^\S|^  [\w-]+:|\Z)", compose
            ).group(1)
            self.assertNotIn("env_file:", block)
            self.assertIn("    environment:\n      TZ: ${TZ:-UTC}\n", block)
            self.assertNotIn(".env.shared", block)
            self.assertNotIn("BROWSER_TOKEN", block)

    def test_http_top_direct_normalizes_ips_and_keeps_acme_exception(self):
        rendered = GW.render_http_top(config())
        self.assertIn("default 0;", rendered)
        self.assertIn("203.0.113.0/24 1;", rendered)
        self.assertIn("198.51.100.7/32 1;", rendered)
        self.assertIn('"~^0:/\\.well-known/acme-challenge/" 0;', rendered)
        self.assertIn('"~^1:" 0;', rendered)
        self.assertIn("default 1;", rendered)

    def test_acme_exception_uses_normalized_uri_and_exact_prefix(self):
        rendered = GW.render_http_top(config())
        variable = re.search(r'map "\$dtw_ip_ok:(\$\w+)"', rendered).group(1)
        pattern = re.search(r'"~\^0:([^"\n]+)" 0;', rendered).group(1)
        cases = {
            "/.well-known/acme-challenge/token": True,
            "/.well-known/acme-challenge/token?query=1": True,
            "/.well-known/acme-challenge/../../api/health": False,
            "/.well-known/acme-challenge/%2e%2e/%2e%2e/api/health": False,
            "/.well-known/acme-challenge/..%2f..%2fapi/health": False,
            "/.well-known/acme-challenge.evil/token": False,
            "/xwell-known/acme-challenge/token": False,
        }
        for request_uri, exempt in cases.items():
            with self.subTest(uri=request_uri):
                normalized = posixpath.normpath(urllib.parse.unquote(request_uri.split("?", 1)[0]))
                value = normalized if variable == "$uri" else request_uri
                self.assertEqual(bool(re.match(pattern, value)), exempt)
        self.assertEqual(variable, "$uri")

    def test_forced_ssl_acme_break_follows_allowlist_and_uses_normalized_uri(self):
        template = (ROOT / "docker/webchat/npmplus/_common.conf").read_text()
        forced = template.split("{% if ssl_forced %}", 1)[1]
        guard = "if ($dtw_block) { return 403; }"
        acme = r"if ($uri ~ ^/\.well-known/acme-challenge/)"
        self.assertIn(guard, forced)
        self.assertIn(acme, forced)
        self.assertLess(forced.index(guard), forced.index(acme))
        self.assertLess(forced.index(guard), forced.index("break;"))
        self.assertNotIn("if ($request_uri ~", forced)

    def test_http_top_empty_proxy_allows_all_but_empty_direct_does_not(self):
        self.assertIn(
            "geo $dtw_ip_ok {\n    default 1;",
            GW.render_http_top(config(exposure="behind_proxy", allowed_ips=[])),
        )
        self.assertIn(
            "geo $dtw_ip_ok {\n    default 0;", GW.render_http_top(config(allowed_ips=[]))
        )

    def test_after_conf_exempts_only_four_fields_with_host_method_path_chains(self):
        rendered = GW.render_crs_after(config())
        lines = rendered.splitlines()
        self.assertEqual(len(lines), 12)
        fields = ("json.text", "json.content", "q", "json.password")
        methods = ("POST", "PUT", "GET", "POST")
        for index, field in enumerate(fields):
            start, method, path = lines[index * 3 : index * 3 + 3]
            self.assertIn(f"id:{9600100 + index},", start)
            self.assertIn(r"@rx ^chat\.example\.com(?::[0-9]+)?$", start)
            self.assertIn(f"@streq {methods[index]}", method)
            self.assertIn("chain", method)
            self.assertIn(f"attack-rce;ARGS:{field}", path)
            self.assertIn(f"attack-lfi;ARGS:{field}", path)
            self.assertEqual("attack-xss" in path, index != 2)
            self.assertEqual("attack-sqli" in path, index != 2)
        for field in ("json.name", "json.secret", "json.code", "json.confirm_name"):
            self.assertNotIn(field, rendered)
        self.assertIn(r"^/api/topics/-?[0-9]+(?:\.[0-9]+)?/messages$", rendered)
        self.assertIn(r"(?:PERSONA|BEHAVIOR|MAINMEMORY|SHAREDMEMORY)\.md$", rendered)

    def test_crs_block_has_exact_three_scanner_filters_and_no_pre_eval(self):
        appsec = GW.render_appsec()
        self.assertIn("name: custom/dtw-crs\n", appsec)
        self.assertIn("inband_rules:\n  - crowdsecurity/crs", appsec)
        self.assertNotIn("pre_eval:", appsec)
        self.assertNotIn("outofband_rules:", appsec)
        self.assertEqual(appsec.count("  - filter:"), 3)
        self.assertEqual(appsec.count("- CancelAlert()"), 2)
        self.assertEqual(appsec.count("- SendAlert()"), 1)
        self.assertIn('!any(evt.Appsec.MatchedRules, #.name == "native_rule:913100")', appsec)
        self.assertEqual(len(GW.render_crs_setup().splitlines()), 5)
        self.assertIn(
            "allowed_methods=GET HEAD POST OPTIONS PUT PATCH DELETE", GW.render_crs_setup()
        )
        self.assertIn("crowdsecurity/appsec-default\n  - custom/dtw-crs", GW.render_acquisition())

    def test_env_upsert_preserves_other_lines_and_deduplicates_owned_keys(self):
        original = "# keep\nDUCTOR_UID=1234\nCUSTOM='unchanged' # note\nexport COMPOSE_PROFILES='browser,other'\nDTW_DOMAIN=old\nDTW_DOMAIN=duplicate\n"
        result = GW.update_env(original, {"DTW_DOMAIN": "chat.example.com"}, enable=True)
        self.assertIn("# keep\nDUCTOR_UID=1234\nCUSTOM='unchanged' # note\n", result)
        self.assertEqual(GW.env_values(result)["COMPOSE_PROFILES"], "browser,other,webchat")
        self.assertEqual(result.count("DTW_DOMAIN="), 1)
        self.assertEqual(
            GW.update_env(result, {"DTW_DOMAIN": "chat.example.com"}, enable=True), result
        )
        down = GW.update_env(result, {}, enable=False)
        self.assertEqual(GW.env_values(down)["COMPOSE_PROFILES"], "browser,other")
        self.assertEqual(GW.env_values(down)["DTW_DOMAIN"], "chat.example.com")

    def test_env_upsert_appends_to_missing_newline_and_empty_profiles(self):
        result = GW.update_env("CUSTOM=value", {}, enable=True)
        self.assertEqual(result, 'CUSTOM=value\nCOMPOSE_PROFILES="webchat"\n')
        self.assertEqual(
            GW.env_values(GW.update_env(result, {}, enable=False))["COMPOSE_PROFILES"], ""
        )

    def test_atomic_write_is_private_and_replace_failure_preserves_original(self):
        path = self.repo / "private"
        GW.atomic_write(path, "first")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with patch.object(GW.os, "replace", side_effect=OSError), self.assertRaises(OSError):
            GW.atomic_write(path, "second")
        self.assertEqual(path.read_text(), "first")
        self.assertEqual(list(self.repo.iterdir()), [path])

    def test_settings_proxy_binds_internal_address_trusts_configured_proxies_and_disables_ipv6(
        self,
    ):
        settings = GW.compose_settings(
            config(
                exposure="behind_proxy",
                trusted_proxies=["198.51.100.7/32"],
                bind_address="127.0.0.1",
            )
        )
        self.assertEqual(settings["DTW_NPM_IPV4_BINDING"], "127.0.0.1")
        self.assertNotIn("DTW_TRUST_IP", settings)
        self.assertEqual(settings["DTW_NPM_DISABLE_IPV6"], "true")
        self.assertNotIn("DTW_NPM_DISABLE_HTTPS", settings)
        self.assertNotIn(
            "DTW_TRUST_IP", GW.compose_settings(config(trusted_proxies=["198.51.100.7"]))
        )

    def checked(self, *, errors=None, enabled=True, chat_log=True, returncode=0):
        return completed(
            json.dumps(
                {
                    "errors": errors or [],
                    "chat_log": {"enabled": chat_log},
                    "webchat": config(enabled=enabled),
                }
            ),
            returncode,
        )

    def test_preflight_uses_cli_contract_and_checks_direct_ports(self):
        token = self.home / "webchat/share/bridge.token"
        token.parent.mkdir(parents=True)
        token.write_text("synthetic")
        with (
            patch.object(self.gateway, "compose", return_value=self.checked()) as compose,
            patch.object(self.gateway, "check_ports") as ports,
        ):
            self.gateway.preflight()
        compose.assert_called_once_with(
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
        ports.assert_called_once_with()
        self.assertEqual(self.gateway.environment["DTW_DOMAIN"], "chat.example.com")
        self.assertEqual(token.read_text(), "synthetic")

    def test_preflight_missing_bridge_token_stops_before_startup(self):
        with (
            patch.object(GW, "Gateway", return_value=self.gateway),
            patch.object(self.gateway, "compose", return_value=self.checked()),
            patch.object(self.gateway, "check_ports") as ports,
            patch.object(self.gateway, "verify_npmplus_common_template_source") as verify,
            patch.object(self.gateway, "start_services") as start,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(GW.main(["up"]), 1)
        self.assertIn("bridge_token_missing", stderr.getvalue())
        self.assertIn("restart ductor-bot", stderr.getvalue())
        ports.assert_not_called()
        verify.assert_not_called()
        start.assert_not_called()
        self.assertFalse(self.home.exists())
        self.assertFalse((self.repo / ".env").exists())

    def test_preflight_empty_bridge_token_stops_before_startup(self):
        token = self.home / "webchat/share/bridge.token"
        token.parent.mkdir(parents=True)
        for content in ("", " \t\n"):
            with self.subTest(content=content):
                token.write_text(content)
                with (
                    patch.object(self.gateway, "compose", return_value=self.checked()),
                    patch.object(self.gateway, "check_ports") as ports,
                    self.assertRaisesRegex(GW.GatewayError, "bridge_token_missing"),
                ):
                    self.gateway.preflight()
                ports.assert_not_called()
                self.assertFalse((self.repo / ".env").exists())

    def test_preflight_unreadable_bridge_token_stops_before_startup(self):
        token = self.home / "webchat/share/bridge.token"
        token.parent.mkdir(parents=True)
        token.write_text("synthetic")
        for operation in ("is_file", "read_text"):
            with self.subTest(operation=operation):
                with (
                    patch.object(GW, "Gateway", return_value=self.gateway),
                    patch.object(self.gateway, "compose", return_value=self.checked()),
                    patch.object(Path, operation, side_effect=PermissionError("private detail")),
                    patch.object(self.gateway, "check_ports") as ports,
                    patch.object(self.gateway, "verify_npmplus_common_template_source") as verify,
                    patch.object(self.gateway, "start_services") as start,
                    contextlib.redirect_stderr(io.StringIO()) as stderr,
                ):
                    self.assertEqual(GW.main(["up"]), 1)
                self.assertIn("bridge_token_unreadable", stderr.getvalue())
                self.assertIn("DUCTOR_UID", stderr.getvalue())
                self.assertIn("host UID", stderr.getvalue())
                self.assertNotIn("private detail", stderr.getvalue())
                ports.assert_not_called()
                verify.assert_not_called()
                start.assert_not_called()
                self.assertEqual(token.read_text(), "synthetic")
                self.assertFalse(self.gateway.state.exists())
                self.assertFalse((self.repo / ".env").exists())

    def test_installation_restarts_bot_before_gateway_up(self):
        installation = (ROOT / "docs/installation.md").read_text()
        steps = installation.split("ductor webchat set-password", 1)[1].split("```", 1)[0]
        self.assertLess(
            steps.index("docker compose restart ductor-bot"), steps.index("scripts/webchat.py up")
        )

    def test_preflight_config_errors_return_one_before_any_writes(self):
        with (
            patch.object(GW, "Gateway", return_value=self.gateway),
            patch.object(
                self.gateway,
                "compose",
                return_value=self.checked(errors=["chat_log_required"], returncode=1),
            ),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(GW.main(["up"]), 1)
        self.assertEqual(stderr.getvalue(), "chat_log_required\n")
        self.assertFalse(self.home.exists())
        self.assertFalse((self.repo / ".env").exists())

    def test_preflight_refuses_chat_log_false_even_if_cli_misses_error(self):
        with (
            patch.object(self.gateway, "compose", return_value=self.checked(chat_log=False)),
            self.assertRaisesRegex(GW.GatewayError, "chat_log_required"),
        ):
            self.gateway.preflight()

    def test_preflight_disabled_and_malformed_cli_are_rejected(self):
        for result in (
            self.checked(enabled=False),
            completed("not json", 1),
            completed("{}"),
            self.checked(returncode=1),
        ):
            with (
                self.subTest(result=result.stdout),
                patch.object(self.gateway, "compose", return_value=result),
                self.assertRaises(GW.GatewayError),
            ):
                self.gateway.preflight()
        self.assertFalse(self.home.exists())

    def test_preflight_rejects_shell_domain_override_empty_or_different(self):
        for value in ("", "other.example.com"):
            with (
                self.subTest(value=value),
                patch.object(self.gateway, "compose", return_value=self.checked()),
                self.assertRaisesRegex(GW.GatewayError, "domain_mismatch"),
            ):
                self.gateway.environment["DTW_DOMAIN"] = value
                self.gateway.preflight()

    def test_source_hash_mismatch_fails_before_directory_or_env_writes(self):
        with (
            patch.object(self.gateway, "preflight"),
            patch.object(
                self.gateway,
                "compose",
                return_value=completed(
                    json.dumps(
                        {"services": {"dt-webchat-npm": {"image": "example/image@sha256:test"}}}
                    )
                ),
            ),
            patch.object(
                self.gateway, "run", return_value=completed("wrong /app/templates/_common.conf")
            ),
            self.assertRaisesRegex(GW.GatewayError, "npmplus_template_source_mismatch"),
        ):
            self.gateway.up()
        self.assertFalse(self.home.exists())
        self.assertFalse((self.repo / ".env").exists())

    def test_direct_ports_refuse_foreign_listener_and_accept_own_running_npm(self):
        with (
            patch.object(self.gateway, "compose", return_value=completed()),
            patch.object(GW, "port_is_listening", return_value=True),
            self.assertRaisesRegex(GW.GatewayError, "direct_port_in_use"),
        ):
            self.gateway.check_ports()
        info = [
            {
                "State": {"Running": True},
                "Config": {"Env": ["HTTP_PORT=80", "HTTPS_PORT=443", "IPV4_BINDING=0.0.0.0"]},
            }
        ]
        with (
            patch.object(self.gateway, "compose", return_value=completed("own-container")),
            patch.object(self.gateway, "run", return_value=completed(json.dumps(info))),
            patch.object(GW, "port_is_listening", return_value=True),
        ):
            self.gateway.check_ports()

    def test_direct_ports_check_unprivileged_listener_tables(self):
        header = "  sl  local_address rem_address   st\n"
        for table in ("tcp", "tcp6"):
            for state, busy in (("0A", True), ("01", False), ("06", False), ("08", False)):
                with self.subTest(table=table, state=state):
                    address = "00000000" if table == "tcp" else "0" * 32
                    row = f"0: {address}:0050 {address}:0000 {state}\n"
                    contents = {"tcp": header, "tcp6": header}
                    contents[table] += row
                    with (
                        patch.object(self.gateway, "compose", return_value=completed()),
                        patch.object(
                            Path,
                            "read_text",
                            autospec=True,
                            side_effect=lambda path, tables=contents: tables[path.name],
                        ),
                    ):
                        if busy:
                            with self.assertRaisesRegex(GW.GatewayError, "direct_port_in_use"):
                                self.gateway.check_ports()
                        else:
                            self.gateway.check_ports()

    def test_direct_ports_with_no_listeners_never_bind(self):
        with (
            patch.object(self.gateway, "compose", return_value=completed()),
            patch("socket.socket") as socket,
            patch.object(Path, "read_text", return_value="sl local_address rem_address st\n"),
        ):
            self.gateway.check_ports()
        socket.assert_not_called()

    def prepare_state(self):
        self.gateway.state.mkdir(parents=True)
        GW.atomic_write(
            self.gateway.state / "npm-admin.secret",
            json.dumps({"identity": "admin@example.com", "password": "synthetic"}),
        )
        template = self.repo / "docker/webchat/npmplus/_common.conf"
        template.parent.mkdir(parents=True)
        template.write_text("template")
        (self.gateway.state / "nginx.conf").parent.mkdir(parents=True, exist_ok=True)
        (self.gateway.state / "nginx.conf").write_text("nginx")

    def test_first_bootstrap_is_transient_and_retired_after_verified_login(self):
        template = self.repo / "docker/webchat/npmplus/_common.conf"
        template.parent.mkdir(parents=True)
        template.write_text("template")
        (self.gateway.state / "nginx.conf").parent.mkdir(parents=True, exist_ok=True)
        (self.gateway.state / "nginx.conf").write_text("nginx")
        self.gateway.state.mkdir(parents=True, exist_ok=True)
        calls = []

        def run(args, **_kwargs):
            calls.append((args, dict(self.gateway.environment)))
            return completed("[]")

        with (
            patch.object(self.gateway, "initialized", return_value=False),
            patch.object(self.gateway, "run", side_effect=run),
            patch.object(self.gateway, "wait_healthy"),
            patch.object(self.gateway, "login") as login,
        ):
            self.gateway.start_services()
        bootstrap = [(args, env) for args, env in calls if "-f" in args]
        self.assertEqual(len(bootstrap), 1)
        self.assertIn("INITIAL_ADMIN_PASSWORD", bootstrap[0][1])
        self.assertTrue(
            all("INITIAL_ADMIN_PASSWORD" not in arg for args, _env in calls for arg in args)
        )
        self.assertNotIn("INITIAL_ADMIN_PASSWORD", self.gateway.environment)
        self.assertFalse((self.gateway.state / "bootstrap.compose.json").exists())
        self.assertEqual(login.call_count, 2)
        self.assertEqual((self.gateway.state / "npm-admin.secret").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.gateway.state / "common-template.sha256").exists())
        self.assertFalse((self.gateway.state / "nginx-config.sha256").exists())

    def test_changed_nginx_policy_recreates_container_but_unchanged_policy_does_not(self):
        self.prepare_state()
        with (
            patch.multiple(
                self.gateway,
                preflight=lambda: None,
                verify_npmplus_common_template_source=lambda: None,
                verified_nginx_config=lambda: (self.gateway.state / "nginx.conf").read_text(),
                crowdsec=lambda: None,
                nginx_config=lambda: None,
                health=lambda: None,
            ),
            patch.object(self.gateway, "initialized", return_value=True),
            patch.object(self.gateway, "compose") as compose,
            patch.object(self.gateway, "run", return_value=completed("[]")),
            patch.object(self.gateway, "wait_healthy"),
            patch.object(self.gateway, "login"),
            patch.object(
                self.gateway,
                "npm_request",
                return_value=[dict(GW.proxy_payload(config(), 7), id=4)],
            ),
        ):
            self.gateway.up()
            compose.reset_mock()
            self.gateway.start_services()
            self.assertFalse(
                any("--force-recreate" in call.args for call in compose.call_args_list)
            )
            (self.gateway.state / "nginx.conf").write_text("changed policy")
            compose.reset_mock()
            self.gateway.start_services()
            self.assertTrue(any("--force-recreate" in call.args for call in compose.call_args_list))

    def test_changed_gateway_config_regenerates_matching_host_and_commits_hashes(self):
        self.prepare_state()
        host = dict(GW.proxy_payload(config(), 7), id=4)
        hash_files = [
            self.gateway.state / "common-template.sha256",
            self.gateway.state / "nginx-config.sha256",
        ]
        sources = [
            self.repo / "docker/webchat/npmplus/_common.conf",
            self.gateway.state / "nginx.conf",
        ]
        with (
            patch.multiple(
                self.gateway,
                preflight=lambda: None,
                verify_npmplus_common_template_source=lambda: None,
                verified_nginx_config=lambda: (self.gateway.state / "nginx.conf").read_text(),
                crowdsec=lambda: None,
                nginx_config=lambda: None,
                health=lambda: None,
            ),
            patch.object(self.gateway, "initialized", return_value=True),
            patch.object(self.gateway, "compose"),
            patch.object(self.gateway, "run", return_value=completed("[]")),
            patch.object(self.gateway, "wait_healthy"),
            patch.object(self.gateway, "login"),
        ):
            for changed_source in [None, *sources]:
                with self.subTest(changed_source=changed_source):
                    if changed_source:
                        changed_source.write_text("changed")
                    previous = [p.read_text() if p.exists() else None for p in hash_files]
                    self.gateway.start_services()
                    self.assertEqual(
                        [p.read_text() if p.exists() else None for p in hash_files], previous
                    )
                    with patch.object(self.gateway, "npm_request", side_effect=[[host], {}]) as api:
                        self.gateway.up()
                    self.assertEqual(api.call_count, 2)
                    self.assertEqual(
                        api.call_args.args, ("/nginx/proxy-hosts/4", GW.proxy_payload(config(), 7))
                    )
                    self.assertEqual(api.call_args.kwargs, {"method": "PUT"})
                    for source, hash_file in zip(sources, hash_files):
                        self.assertEqual(
                            hash_file.read_text().strip(),
                            hashlib.sha256(source.read_bytes()).hexdigest(),
                        )
                    self.gateway.start_services()
                    with patch.object(self.gateway, "npm_request", return_value=[host]) as api:
                        self.gateway.up()
                    api.assert_called_once_with("/nginx/proxy-hosts")

    def test_proxy_regeneration_failure_does_not_commit_hashes_and_next_start_retries(self):
        self.prepare_state()
        host = dict(GW.proxy_payload(config(), 7), id=4)
        hash_files = [
            self.gateway.state / "common-template.sha256",
            self.gateway.state / "nginx-config.sha256",
        ]
        with (
            patch.multiple(
                self.gateway,
                preflight=lambda: None,
                verify_npmplus_common_template_source=lambda: None,
                verified_nginx_config=lambda: (self.gateway.state / "nginx.conf").read_text(),
                crowdsec=lambda: None,
                nginx_config=lambda: None,
                health=lambda: None,
            ),
            patch.object(self.gateway, "initialized", return_value=True),
            patch.object(self.gateway, "compose"),
            patch.object(self.gateway, "run", return_value=completed("[]")),
            patch.object(self.gateway, "wait_healthy"),
            patch.object(self.gateway, "login"),
        ):
            for previous in (None, "old hash\n"):
                with self.subTest(previous=previous):
                    if previous:
                        for path in hash_files:
                            path.write_text(previous)
                    self.gateway.start_services()
                    with (
                        patch.object(
                            self.gateway,
                            "npm_request",
                            side_effect=[[host], GW.GatewayError("npm_api_failed")],
                        ),
                        self.assertRaisesRegex(GW.GatewayError, "npm_api_failed"),
                    ):
                        self.gateway.up()
                    self.assertEqual(
                        [p.read_text() if p.exists() else None for p in hash_files], [previous] * 2
                    )
                    retry = GW.Gateway(self.repo)
                    retry.config = config()
                    with (
                        patch.multiple(
                            retry,
                            preflight=lambda: None,
                            verify_npmplus_common_template_source=lambda: None,
                            verified_nginx_config=lambda: "nginx",
                            crowdsec=lambda: None,
                            nginx_config=lambda: None,
                            health=lambda: None,
                        ),
                        patch.object(retry, "initialized", return_value=True),
                        patch.object(retry, "compose") as retry_compose,
                        patch.object(retry, "run", return_value=completed("[]")),
                        patch.object(retry, "wait_healthy"),
                        patch.object(retry, "login"),
                        patch.object(retry, "npm_request", side_effect=[[host], {}]) as api,
                    ):
                        retry.up()
                    self.assertTrue(
                        any(
                            "--force-recreate" in call.args for call in retry_compose.call_args_list
                        )
                    )
                    self.assertEqual(api.call_args.kwargs, {"method": "PUT"})
                    for path in hash_files:
                        path.unlink()

    def test_existing_database_without_admin_secret_stops(self):
        self.gateway.state.mkdir(parents=True)
        with (
            patch.object(self.gateway, "initialized", return_value=True),
            self.assertRaisesRegex(GW.GatewayError, "npm_admin_secret_missing"),
        ):
            self.gateway.start_services()

    def test_existing_bootstrap_env_is_rejected_after_retirement(self):
        self.prepare_state()
        with (
            patch.object(self.gateway, "initialized", return_value=True),
            patch.object(self.gateway, "compose", return_value=completed("container")),
            patch.object(
                self.gateway, "run", return_value=completed('["INITIAL_ADMIN_PASSWORD=synthetic"]')
            ),
            patch.object(self.gateway, "wait_healthy"),
            patch.object(self.gateway, "login"),
            self.assertRaisesRegex(GW.GatewayError, "bootstrap_retirement_failed"),
        ):
            self.gateway.start_services()

    def test_proxy_create_direct_reuses_existing_certificate(self):
        replies = [
            [],
            {"id": 4},
            [{"id": 7, "provider": "letsencrypt", "domain_names": ["chat.example.com"]}],
            {},
        ]
        with patch.object(self.gateway, "npm_request", side_effect=replies) as api:
            self.gateway.proxy_host()
        create = api.call_args_list[1]
        self.assertEqual(create.args[0], "/nginx/proxy-hosts")
        self.assertEqual(create.args[1]["forward_port"], 8744)
        self.assertNotIn("access_list_id", create.args[1])
        self.assertEqual(
            create.args[1]["advanced_config"],
            "if ($dtw_block) { return 403; }\n" + GW.PROXY_HOST_PENDING_MARKER,
        )
        update = api.call_args_list[-1].args[1]
        self.assertEqual(update["certificate_id"], 7)
        self.assertNotIn("access_list_id", update)
        self.assertTrue(update["ssl_forced"] and update["http2_support"] and update["hsts_enabled"])
        self.assertFalse(update["npmplus_crowdsec_appsec"])

    def test_proxy_create_direct_issues_certificate_only_if_missing(self):
        with patch.object(
            self.gateway, "npm_request", side_effect=[[], {"id": 4}, [], {"id": 7}, {}]
        ) as api:
            self.gateway.proxy_host()
        self.assertEqual(api.call_args_list[3].args[0], "/nginx/certificates")
        self.assertEqual(api.call_args_list[3].args[1]["meta"], {"dns_challenge": False})

    def test_certificate_failure_removes_only_new_proxy_host(self):
        for existing in (False, True):
            for stage in ("list", "issue", "attach"):
                with self.subTest(existing=existing, stage=stage):
                    host = dict(GW.proxy_payload(config()), id=4)
                    replies = [[host]] if existing else [[], {"id": 4}]
                    if stage != "list":
                        replies.append([])
                    if stage == "attach":
                        replies.append({"id": 7})
                    replies.append(GW.GatewayError("npm_api_failed"))
                    if not existing:
                        replies.append(None)
                    with (
                        patch.object(self.gateway, "npm_request", side_effect=replies) as api,
                        self.assertRaisesRegex(GW.GatewayError, "npm_api_failed"),
                    ):
                        self.gateway.proxy_host()
                    deletions = [
                        call for call in api.call_args_list if call.kwargs.get("method") == "DELETE"
                    ]
                    self.assertEqual(len(deletions), int(not existing))
                    if deletions:
                        self.assertEqual(deletions[0].args, ("/nginx/proxy-hosts/4",))

    def test_certificate_cleanup_failure_preserves_error_and_retries_cleanup(self):
        with (
            patch.object(
                self.gateway,
                "npm_request",
                side_effect=[
                    [],
                    {"id": 4},
                    GW.GatewayError("certificate_issue_failed"),
                    GW.GatewayError("npm_api_failed"),
                ],
            ) as api,
            self.assertRaisesRegex(
                GW.GatewayError, "certificate_issue_failed.*proxy_host_cleanup_failed"
            ),
        ):
            self.gateway.proxy_host()
        pending = dict(api.call_args_list[1].args[1], id=4)
        for cleanup_fails in (True, False):
            with self.subTest(cleanup_fails=cleanup_fails):
                replies = [[pending]]
                if cleanup_fails:
                    replies.append(GW.GatewayError("npm_api_failed"))
                else:
                    replies.extend([None, {"id": 5}, [], {"id": 7}, {}])
                with patch.object(self.gateway, "npm_request", side_effect=replies) as retry:
                    if cleanup_fails:
                        with self.assertRaisesRegex(GW.GatewayError, "npm_api_failed"):
                            self.gateway.proxy_host()
                    else:
                        self.gateway.proxy_host()
                self.assertEqual(retry.call_args_list[1].args, ("/nginx/proxy-hosts/4",))
                self.assertEqual(retry.call_args_list[1].kwargs, {"method": "DELETE"})
                if cleanup_fails:
                    self.assertEqual(retry.call_count, 2)
                else:
                    self.assertEqual(retry.call_args_list[2].args[0], "/nginx/proxy-hosts")
                    self.assertEqual(
                        retry.call_args_list[-1].args[1], GW.proxy_payload(config(), 7)
                    )

    def test_proxy_existing_matching_host_is_idempotent(self):
        payload = GW.proxy_payload(config(), 7)
        with patch.object(
            self.gateway, "npm_request", return_value=[dict(payload, id=4, access_list_id=0)]
        ) as api:
            self.gateway.proxy_host()
        api.assert_called_once_with("/nginx/proxy-hosts")

    def test_proxy_existing_host_update_omits_read_only_access_list_id(self):
        host = dict(GW.proxy_payload(config(), 7), id=4, access_list_id=0, forward_port=9999)
        with patch.object(self.gateway, "npm_request", side_effect=[[host], {}]) as api:
            self.gateway.proxy_host()
        update = api.call_args_list[-1]
        self.assertEqual(update.kwargs, {"method": "PUT"})
        self.assertNotIn("access_list_id", update.args[1])
        self.assertEqual(update.args[1]["forward_port"], 8744)

    def test_proxy_behind_proxy_has_no_certificate_or_redirect(self):
        self.gateway.config = config(exposure="behind_proxy")
        with patch.object(self.gateway, "npm_request", side_effect=[[], {"id": 4}]) as api:
            self.gateway.proxy_host()
        self.assertEqual(api.call_count, 2)
        payload = api.call_args_list[-1].args[1]
        self.assertEqual(payload["certificate_id"], 0)
        self.assertFalse(payload["ssl_forced"])
        self.assertTrue(payload["block_exploits"] and payload["allow_websocket_upgrade"])

    def test_crowdsec_unchanged_does_not_reload_and_uses_saved_bouncer(self):
        self.gateway.state.mkdir(parents=True)
        GW.atomic_write(self.gateway.state / "bouncer.key", "synthetic")
        replies = [
            completed("crowdsecurity/appsec-crs,enabled"),
            completed(),
            completed(
                "name,ip,revoked,last_pull,type,version,auth_type\n"
                "dt-webchat-npm,198.51.100.7,validated,2026-11-11T01:00:00Z,"
                "crowdsec-nginx-bouncer,v1.1.6,api-key\n"
            ),
        ]
        with (
            patch.object(self.gateway, "execute", side_effect=replies),
            patch.object(self.gateway, "sync_file", return_value=False),
            patch.object(self.gateway, "compose") as compose,
            patch.object(self.gateway, "probe_appsec") as probe,
        ):
            self.gateway.crowdsec()
        compose.assert_not_called()
        probe.assert_called_once_with("synthetic")

    def test_crowdsec_changed_installs_crs_removes_scenarios_and_restarts(self):
        self.gateway.state.mkdir(parents=True)
        replies = [
            completed(),
            completed(),
            completed("\n".join(s + ",enabled" for s in GW.DISABLED_SCENARIOS)),
            *[completed() for _ in range(3)],
            completed(),
            completed("synthetic"),
        ]
        with (
            patch.object(self.gateway, "execute", side_effect=replies) as execute,
            patch.object(self.gateway, "sync_file", return_value=True),
            patch.object(self.gateway, "compose") as compose,
            patch.object(self.gateway, "wait_healthy"),
            patch.object(self.gateway, "probe_appsec"),
        ):
            self.gateway.crowdsec()
        self.assertIn(
            ("dt-webchat-crowdsec", "cscli", "collections", "install", "crowdsecurity/appsec-crs"),
            [call.args for call in execute.call_args_list],
        )
        for scenario in GW.DISABLED_SCENARIOS:
            self.assertIn(
                ("dt-webchat-crowdsec", "cscli", "scenarios", "remove", scenario, "--force"),
                [call.args for call in execute.call_args_list],
            )
        self.assertEqual(
            [call.args for call in compose.call_args_list],
            [("kill", "-s", "HUP", "dt-webchat-crowdsec"), ("restart", "dt-webchat-npm")],
        )
        self.assertEqual((self.gateway.state / "bouncer.key").read_text(), "synthetic")

    def test_health_web_200_and_gateway_200_or_geo_403_are_accepted(self):
        for status in ("200", "403"):
            with (
                self.subTest(status=status),
                patch.object(
                    self.gateway,
                    "run",
                    side_effect=[
                        completed("200"),
                        completed(f"HTTP/1.1 {status}\r\n\r\n\n{status}"),
                    ],
                ) as run,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.gateway.health()
            self.assertIn(f"gateway_health_status={status}", output.getvalue())
            self.assertIn("chat.example.com:443:127.0.0.1", run.call_args.args[0])
            self.assertTrue(
                all(call.args[0][1:3] == ["--noproxy", "*"] for call in run.call_args_list)
            )

    def test_health_rejects_auth_challenge_5xx_and_connection_failure(self):
        for response in (
            completed("\n500"),
            completed("WWW-Authenticate: Basic realm=test\r\n\n403"),
            completed("\n000", 7),
            completed("\n302"),
        ):
            clock = FakeClock()
            with (
                self.subTest(response=response),
                patch.object(
                    self.gateway,
                    "run",
                    side_effect=lambda args, reply=response, **_kwargs: (
                        completed("200") if args[-1].startswith("http://") else reply
                    ),
                ),
                patch.object(GW.time, "monotonic", side_effect=clock.monotonic),
                patch.object(GW.time, "sleep", side_effect=clock.sleep),
                contextlib.redirect_stdout(io.StringIO()) as output,
                self.assertRaisesRegex(GW.GatewayError, "gateway_health_failed"),
            ):
                self.gateway.health()
            self.assertEqual(clock.now, GW.HEALTH_TIMEOUT_SECONDS)
            self.assertEqual(len(output.getvalue().splitlines()), 2)
        clock = FakeClock()
        with (
            patch.object(self.gateway, "run", return_value=completed("503")),
            patch.object(GW.time, "monotonic", side_effect=clock.monotonic),
            patch.object(GW.time, "sleep", side_effect=clock.sleep),
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaisesRegex(GW.GatewayError, "web_health_failed"),
        ):
            self.gateway.health()
        self.assertEqual(clock.now, GW.HEALTH_TIMEOUT_SECONDS)
        self.assertEqual(output.getvalue(), "web_health_status=503\n")

    def test_health_retries_gateway_connection_failures_until_geo_403(self):
        clock = FakeClock()
        with (
            patch.object(
                self.gateway,
                "run",
                side_effect=[
                    completed("200"),
                    completed("\n000", 60),
                    completed("\n000", 7),
                    completed("HTTP/1.1 403\r\n\r\n\n403"),
                ],
            ) as run,
            patch.object(GW.time, "monotonic", side_effect=clock.monotonic),
            patch.object(GW.time, "sleep", side_effect=clock.sleep),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.gateway.health()
        self.assertEqual(clock.now, 2)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(output.getvalue(), "web_health_status=200\ngateway_health_status=403\n")

    def test_health_retries_web_app_before_gateway(self):
        clock = FakeClock()
        with (
            patch.object(
                self.gateway,
                "run",
                side_effect=[
                    completed("\n000", 7),
                    completed("503"),
                    completed("200"),
                    completed("\n200"),
                ],
            ) as run,
            patch.object(GW.time, "monotonic", side_effect=clock.monotonic),
            patch.object(GW.time, "sleep", side_effect=clock.sleep),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.gateway.health()
        self.assertEqual(clock.now, 2)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(output.getvalue(), "web_health_status=200\ngateway_health_status=200\n")

    def test_health_timeout_includes_curl_duration_and_prints_only_last_code(self):
        clock = FakeClock()
        timeouts = []

        def run(args, **_kwargs):
            if args[-1].startswith("http://"):
                return completed("200")
            timeout = float(args[args.index("--max-time") + 1])
            timeouts.append(timeout)
            clock.now += timeout
            return completed("secret response header\n000", 28)

        with (
            patch.object(self.gateway, "run", side_effect=run),
            patch.object(GW.time, "monotonic", side_effect=clock.monotonic),
            patch.object(GW.time, "sleep", side_effect=clock.sleep),
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaisesRegex(GW.GatewayError, "gateway_health_failed"),
        ):
            self.gateway.health()
        self.assertEqual(clock.now, GW.HEALTH_TIMEOUT_SECONDS)
        self.assertEqual(timeouts, [20, 9])
        self.assertEqual(output.getvalue(), "web_health_status=200\ngateway_health_status=000\n")

    def test_down_keeps_volumes_and_state_and_removes_only_webchat_profile(self):
        self.prepare_state()
        (self.repo / ".env").write_text("COMPOSE_PROFILES=other,webchat\nCUSTOM=keep\n")
        with (
            patch.object(self.gateway, "compose") as compose,
            patch.object(self.gateway, "run") as run,
        ):
            self.gateway.down()
        self.assertEqual(
            [call.args for call in compose.call_args_list],
            [("stop", *GW.SERVICES), ("rm", "-f", *GW.SERVICES)],
        )
        run.assert_not_called()
        self.assertTrue(self.gateway.state.exists())
        self.assertEqual(
            GW.env_values((self.repo / ".env").read_text())["COMPOSE_PROFILES"], "other"
        )
        self.assertIn("CUSTOM=keep", (self.repo / ".env").read_text())

    def test_down_purge_removes_only_named_gateway_volumes_and_state(self):
        self.prepare_state()
        with (
            patch.object(self.gateway, "compose"),
            patch.object(self.gateway, "run", return_value=completed()) as run,
        ):
            self.gateway.down(purge=True)
        self.assertEqual(run.call_args.args[0], ["docker", "volume", "rm", *GW.VOLUMES])
        self.assertFalse(self.gateway.state.exists())

    def test_settings_direct_keeps_public_bindings_and_ipv6(self):
        settings = GW.compose_settings(config())
        self.assertEqual(settings["DTW_NPM_IPV4_BINDING"], "0.0.0.0")  # noqa: S104 - public binding contract
        self.assertEqual(settings["DTW_NPM_IPV6_BINDING"], "[::]")
        self.assertEqual(settings["DTW_NPM_DISABLE_IPV6"], "false")
        self.assertEqual(settings["DTW_NPM_HTTP_PORT"], "80")
        self.assertEqual(settings["DTW_NPM_HTTPS_PORT"], "443")
        self.assertNotIn("DTW_NPM_DISABLE_HTTPS", settings)

    def test_down_purge_is_idempotent_when_volumes_are_already_absent(self):
        with (
            patch.object(self.gateway, "compose"),
            patch.object(self.gateway, "run", return_value=completed(returncode=1)) as run,
        ):
            self.gateway.down(purge=True)
        self.assertEqual(len(run.call_args_list), 3)
        self.assertTrue(
            all(call.args[0][:3] == ["docker", "volume", "inspect"] for call in run.call_args_list)
        )

    def test_up_orders_stages_and_preserves_other_env_values_on_repeat(self):
        (self.repo / ".env").write_text("CUSTOM=keep\nCOMPOSE_PROFILES=other\n")
        steps = []

        def start():
            steps.append("services")
            self.assertEqual(self.gateway.state.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.repo / ".env").stat().st_mode & 0o777, 0o600)
            GW.atomic_write(
                self.gateway.state / "npm-admin.secret",
                json.dumps({"identity": "admin@example.com", "password": "synthetic"}),
            )

        patches = {
            "preflight": lambda: steps.append("preflight"),
            "verify_npmplus_common_template_source": lambda: steps.append("template"),
            "verified_nginx_config": lambda: steps.append("nginx_source") or "nginx",
            "start_services": start,
            "crowdsec": lambda: steps.append("crowdsec"),
            "nginx_config": lambda: steps.append("nginx"),
            "login": lambda _credentials: steps.append("login"),
            "proxy_host": lambda: steps.append("proxy"),
            "health": lambda: steps.append("health"),
        }
        with patch.multiple(self.gateway, **patches):
            self.gateway.up()
            first = (self.repo / ".env").read_text()
            self.gateway.up()
        self.assertEqual(
            steps,
            [
                "preflight",
                "template",
                "nginx_source",
                "services",
                "crowdsec",
                "nginx",
                "login",
                "proxy",
                "health",
            ]
            * 2,
        )
        self.assertEqual((self.repo / ".env").read_text(), first)
        self.assertEqual(GW.env_values(first)["COMPOSE_PROFILES"], "other,webchat")
        self.assertIn("CUSTOM=keep\n", first)
        self.assertEqual(GW.env_values(first)["DTW_DOMAIN"], "chat.example.com")

    def test_up_twice_reuses_bouncer_from_real_csv_format(self):
        added = []

        def execute(_service, *args, **_kwargs):
            if args[:3] == ("cscli", "collections", "list"):
                return completed("crowdsecurity/appsec-crs,enabled\n")
            if args[:3] == ("cscli", "bouncers", "list"):
                header = "name,ip,revoked,last_pull,type,version,auth_type\n"
                row = '"dt-webchat-npm",198.51.100.7,validated,,crowdsec-nginx-bouncer,v1.1.6,api-key\n'
                return completed(header + (row if added else ""))
            if args[:3] == ("cscli", "bouncers", "add"):
                self.assertEqual(added, [])
                added.append(args[3])
                return completed("synthetic")
            return completed()

        def start():
            GW.atomic_write(
                self.gateway.state / "npm-admin.secret",
                json.dumps({"identity": "admin@example.com", "password": "synthetic"}),
            )

        with (
            patch.object(self.gateway, "preflight"),
            patch.object(self.gateway, "verify_npmplus_common_template_source"),
            patch.object(self.gateway, "verified_nginx_config", return_value="nginx"),
            patch.object(self.gateway, "start_services", side_effect=start),
            patch.object(self.gateway, "execute", side_effect=execute),
            patch.object(self.gateway, "sync_file", return_value=False),
            patch.object(self.gateway, "probe_appsec"),
            patch.object(self.gateway, "nginx_config"),
            patch.object(self.gateway, "login"),
            patch.object(self.gateway, "proxy_host"),
            patch.object(self.gateway, "health"),
        ):
            self.gateway.up()
            self.gateway.up()
        self.assertEqual(added, ["dt-webchat-npm"])
        self.assertEqual((self.gateway.state / "bouncer.key").read_text(), "synthetic")

    def test_nginx_update_restores_previous_file_on_syntax_or_reload_failure(self):
        directory = self.repo / "nginx"
        directory.mkdir()
        config_file = directory / "http_top.conf"
        nginx = directory / "nginx"
        nginx.write_text(
            '#!/bin/sh\nif [ ! -f "$NGINX_MARKER" ] && [ "$*" = "$NGINX_FAIL" ]; then touch "$NGINX_MARKER"; exit 1; fi\nexit 0\n'
        )
        nginx.chmod(0o755)
        script = GW.NGINX_UPDATE.replace(
            "/data/custom_nginx/http_top.conf", str(config_file)
        ).replace("/data/custom_nginx", str(directory))
        for fail in ("-t", "-s reload"):
            config_file.write_text("previous\n")
            marker = directory / "failed"
            marker.unlink(missing_ok=True)
            result = subprocess.run(
                ["sh", "-eu", "-c", script],
                input="new\n",
                text=True,
                capture_output=True,
                check=False,
                env={
                    "PATH": f"{directory}:/usr/bin:/bin",
                    "NGINX_FAIL": fail,
                    "NGINX_MARKER": str(marker),
                },
            )
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config_file.read_text(), "previous\n")

    def test_no_secret_in_subprocess_error_output(self):
        with (
            patch.object(GW.subprocess, "run", return_value=completed("synthetic-secret", 1)),
            self.assertRaisesRegex(GW.GatewayError, "^gateway_command_failed$"),
        ):
            self.gateway.run(["fake"])


if __name__ == "__main__":
    unittest.main()
