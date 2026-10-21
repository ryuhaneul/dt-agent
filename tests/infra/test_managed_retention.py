"""The image includes a validated retention policy before switching users."""

import json
from pathlib import Path


def test_managed_retention_policy_is_installed_before_runtime_user() -> None:
    root = Path(__file__).resolve().parents[2]
    source = "docker/ductor-bot/managed-settings/10-dt-agent-retention.json"
    target = "/etc/claude-code/managed-settings.d/10-dt-agent-retention.json"
    content = (root / source).read_text(encoding="utf-8")
    assert content == '{"cleanupPeriodDays": 36500}\n'
    value = json.loads(content)
    assert type(value["cleanupPeriodDays"]) is int
    dockerfile = (root / "docker/ductor-bot/Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.index(f"COPY {source} {target}") < dockerfile.index("USER ductor")
