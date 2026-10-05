"""Cancellation helpers shared by long-running agent operations."""

from __future__ import annotations

import asyncio


def task_is_cancelling() -> bool:
    """Return True when the current task has an outstanding external cancel.

    Used to tell an external cancellation (``/stop``, request abort) apart from
    a ``CancelledError`` that a library's own timeout scope leaked into the
    caller. Only the former must propagate.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
