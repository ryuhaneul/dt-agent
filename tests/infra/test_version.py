"""Tests for installed version display."""

from __future__ import annotations

import importlib.metadata
from unittest.mock import patch

from ductor_bot.infra.version import get_current_version


class TestGetCurrentVersion:
    """Test installed version detection."""

    def test_returns_installed_version(self) -> None:
        with patch("ductor_bot.infra.version.importlib.metadata.version", return_value="1.5.0"):
            assert get_current_version() == "1.5.0"

    def test_returns_fallback_when_not_installed(self) -> None:
        with patch(
            "ductor_bot.infra.version.importlib.metadata.version",
            side_effect=importlib.metadata.PackageNotFoundError,
        ):
            assert get_current_version() == "0.0.0"
