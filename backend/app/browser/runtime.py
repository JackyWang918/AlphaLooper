"""Safe progress metadata and a cooperative deadline for one worker command."""

import time
from contextlib import contextmanager
from contextvars import ContextVar

COMMAND_BUDGET_SECONDS = 35
RESPONSE_TIMEOUT_SECONDS = 45
_runtime = ContextVar("browser_command_runtime", default=None)


class CommandBudgetExceeded(ValueError):
    """Cooperative deadline; retry is allowed only for known read-only actions."""


@contextmanager
def command_runtime(report, *, seconds=COMMAND_BUDGET_SECONDS):
    token = _runtime.set((time.monotonic() + seconds, report))
    try:
        yield
    finally:
        _runtime.reset(token)


def checkpoint(step):
    runtime = _runtime.get()
    if runtime is None:
        return
    deadline, report = runtime
    report(step)
    if time.monotonic() >= deadline:
        raise CommandBudgetExceeded(
            f"页面操作总耗时超过 {COMMAND_BUDGET_SECONDS} 秒；停在：{step}。请人工核对后恢复，不会重放交易动作。"
        )
