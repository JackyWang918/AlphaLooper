"""Run from backend: uv run python -m scripts.check_ai_diagnostics.

Manually test the configured AI using synthetic data; never notify or trade.
"""

import json

from app.ai_diagnostics import DeepSeekAnalyzer
from app.notifications import _setting


def main():
    analyzer = DeepSeekAnalyzer.from_env(_setting)
    report = analyzer.analyze(
        {
            "test_mode": True,
            "reason": "配置自检：模拟交易页面余额控件暂未加载，等待后仍超时。",
            "limitations": "这是构造的测试数据，没有真实账户、订单、余额或浏览器操作。",
        }
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
