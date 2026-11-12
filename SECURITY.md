# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 1.0.x   | Yes       |
| < 1.0   | No        |

## Reporting a vulnerability

Please do not open a public issue for security problems. Report them privately through GitHub private vulnerability reporting:
https://github.com/ryuhaneul/dt-agent/security/advisories/new

Include the affected version or commit, steps to reproduce, and the impact you observed. Fixes are released as patch versions.

## Scope

In scope: the bot, its Docker and Compose setup, access control (allowed users and groups, sub-agent bots), webhook and API listeners, optional conversation history and delivery queues, the emergency web chat (browser sessions, password/TOTP, CSRF, internal bridge, topic operations and file editing), its NPMplus/CrowdSec gateway configuration, and handling of tokens and secrets.

Web chat is disabled by default and requires conversation recording. Direct exposure
requires a client IP allowlist and TLS; behind a proxy, restrict trusted proxy ranges
and enforce client access at the gateway or trusted ingress. Keep the bot bridge,
web app, gateway administration and CrowdSec ports private. History, queued message
bodies and edit backups contain conversation or configuration content; protect the
persistent home and its backups. See [setup](docs/installation.md#emergency-web-chat-optional)
and [authentication and file-editing limits](docs/modules/webchat.md).

Out of scope: vulnerabilities in the third-party AI CLIs themselves (Claude Code, Codex, Gemini, Grok). Report those to their vendors.
