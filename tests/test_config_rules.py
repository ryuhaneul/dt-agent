"""Seeded config rules match the actual restart classification."""

from pathlib import Path
import re

from ductor_bot.config import AgentConfig
from ductor_bot.config_reload import _HOT_RELOADABLE


def test_all_config_rule_variants_list_actual_restart_fields():
    folder = Path(__file__).resolve().parents[1] / "ductor_bot/_home_defaults/config"
    expected = set(AgentConfig.model_fields) - _HOT_RELOADABLE
    rules = list(folder.glob("RULES-*.md"))
    assert len(rules) == 4
    for path in rules:
        line = next(line for line in path.read_text().splitlines() if line.startswith("5. "))
        listed = set(re.findall(r"`([^`]+)`", line)) - {"/restart"}
        assert listed == expected, path.name
        assert "docker" not in listed
        assert "Unknown/new keys" in line
