"""Infrastructure: PID lock and restart sentinels."""

from ductor_bot.infra.pidlock import acquire_lock, release_lock
from ductor_bot.infra.restart import (
    EXIT_RESTART,
    consume_restart_marker,
    consume_restart_sentinel,
    write_restart_marker,
    write_restart_sentinel,
)

__all__ = [
    "EXIT_RESTART",
    "acquire_lock",
    "consume_restart_marker",
    "consume_restart_sentinel",
    "release_lock",
    "write_restart_marker",
    "write_restart_sentinel",
]
