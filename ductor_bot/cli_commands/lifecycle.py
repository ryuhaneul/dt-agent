"""Bot lifecycle CLI command to start the bot."""

from __future__ import annotations

import asyncio
import sys

from ductor_bot.infra.restart import EXIT_RESTART
from ductor_bot.workspace.paths import resolve_paths


def start_bot(verbose: bool = False) -> None:
    """Load config and start the Telegram bot."""
    import logging

    from ductor_bot.__main__ import load_config, run_telegram
    from ductor_bot.infra.pidlock import acquire_instance_lock
    from ductor_bot.logging_config import setup_logging

    paths = resolve_paths()
    acquire_instance_lock(paths.ductor_home / "bot.lock")
    setup_logging(verbose=verbose, log_dir=paths.logs_dir)
    config = load_config()
    configured_home = resolve_paths(ductor_home=config.ductor_home).ductor_home
    if configured_home.resolve() != paths.ductor_home.resolve():
        acquire_instance_lock(configured_home / "bot.lock")
    from ductor_bot.infra.feature_deps import check_feature_deps

    excluded_subs = check_feature_deps(config, paths)
    from ductor_bot.i18n import init as init_i18n

    init_i18n(config.language)
    if not verbose:
        config_level = getattr(logging, config.log_level.upper(), logging.INFO)
        if config_level != logging.INFO:
            setup_logging(level=config_level, log_dir=paths.logs_dir)
    try:
        exit_code = asyncio.run(run_telegram(config, excluded_subs=excluded_subs))
    except KeyboardInterrupt:
        exit_code = 0
    if exit_code == EXIT_RESTART:
        sys.exit(EXIT_RESTART)
    elif exit_code:
        sys.exit(exit_code)
