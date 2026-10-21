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

In scope: the bot, its Docker and Compose setup, access control (allowed users and groups, sub-agent bots), webhook and API listeners, and handling of tokens and secrets.

Out of scope: vulnerabilities in the third-party AI CLIs themselves (Claude Code, Codex, Gemini, Grok). Report those to their vendors.
