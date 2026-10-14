"""Typed recovery decisions; only explicitly safe observations may be retried."""

from decimal import Decimal

RECOVERY_SECONDS = 120
RETRY_SECONDS = 5
READ_ACTIONS = {"order_readiness", "live_progress", "live_inspect", "live_balance"}


class ObservationNotReady(ValueError):
    kind = "read_retry"


class BalanceNotReady(ObservationNotReady):
    kind = "balance_sync"


class RecoveryExpired(ValueError):
    kind = "manual"


class ExecutionUnknown(ValueError):
    def __init__(self, message, kind="submission_unknown"):
        super().__init__(message)
        self.kind = kind


def category(exc):
    return getattr(exc, "kind", "manual")


def validate_wallet(wallet):
    for key in ("quote_available", "base_available", "quote_total", "base_total"):
        value = wallet.get(key)
        if value is None and key.endswith("total"):
            continue
        try:
            number = Decimal(value)
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise ValueError("账户余额字段无效，无法安全核对。") from exc
        if not number.is_finite() or number < 0:
            raise ValueError("账户余额必须是有限非负数。")
    for asset in ("quote", "base"):
        total = wallet.get(asset + "_total")
        if total is not None and Decimal(total) < Decimal(wallet[asset + "_available"]):
            raise BalanceNotReady("账户可用余额大于总余额，等待页面同步。")


def coherent_delta(side, cash, token):
    """Both legs must move together; zero/zero can be a cancelled unfilled order."""
    if cash == token == 0:
        return True
    return (cash < 0 and token > 0) if side == "buy" else (cash > 0 and token < 0)
