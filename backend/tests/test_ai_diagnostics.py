import asyncio
import json

import httpx
import pytest

from app.ai_diagnostics import (
    DEFAULT_MODEL,
    DeepSeekAnalyzer,
    diagnostic_context,
    safe_text,
)


def diagnosis():
    return {
        "summary": "推测卖出后的 USDT 余额尚未同步。",
        "confidence": "medium",
        "possible_causes": ["两侧余额异步更新"],
        "suggested_steps": ["人工查看平台当前委托，再核对两侧余额，保留原订单记录。"],
        "missing_evidence": ["平台最新余额和实际成交结果"],
    }


def completion(result=None, finish="stop"):
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "content": json.dumps(result or diagnosis(), ensure_ascii=False)
                },
            }
        ]
    }


def test_deepseek_request_is_json_advisory_with_no_tools():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion())

    analyzer = DeepSeekAnalyzer(
        "sk-test-private", transport=httpx.MockTransport(handler)
    )
    context = {"reason": "余额尚未同步", "balances": {"quote_available": "50.00000001"}}
    result = analyzer.analyze(context)
    assert (
        result["status"] == "completed" and result["summary"] == diagnosis()["summary"]
    )
    request = requests[0]
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test-private"
    body = json.loads(request.content)
    assert body["model"] == DEFAULT_MODEL
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    assert body["stream"] is False and body["max_tokens"] == 900
    assert "tools" not in body and "tool_choice" not in body
    assert json.loads(body["messages"][1]["content"]) == context
    assert "不可信" in body["messages"][0]["content"]


@pytest.mark.parametrize(
    "status,expected",
    [(401, "Key 无效"), (402, "余额不足"), (429, "限流"), (503, "不可用")],
)
def test_http_failure_uses_safe_fallback_without_credentials_or_body(status, expected):
    analyzer = DeepSeekAnalyzer(
        "sk-secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, text="sk-secret upstream private response"
            )
        ),
    )
    report = analyzer.analyze({})
    assert report["status"] == "unavailable" and expected in report["message"]
    assert "sk-secret" not in json.dumps(report) and "private" not in json.dumps(report)


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        completion(finish="length"),
        completion({**diagnosis(), "suggested_steps": []}),
        completion({**diagnosis(), "confidence": "certain"}),
        completion({**diagnosis(), "execute": "resume"}),
        completion({**diagnosis(), "summary": "长" * 161}),
    ],
)
def test_malformed_truncated_or_extra_fields_cannot_become_valid_advice(body):
    analyzer = DeepSeekAnalyzer(
        "test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )
    assert analyzer.analyze({})["status"] == "unavailable"


def test_overall_timeout_cancels_slow_api_without_retry():
    calls = []

    async def slow(request):
        calls.append(request)
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=completion())

    analyzer = DeepSeekAnalyzer(
        "test", timeout=0.01, transport=httpx.MockTransport(slow)
    )
    report = analyzer.analyze({})
    assert report["status"] == "unavailable" and "超时" in report["message"]
    assert len(calls) == 1


def test_oversized_response_is_rejected():
    analyzer = DeepSeekAnalyzer(
        "test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b" " * 33000)
        ),
    )
    assert analyzer.analyze({})["status"] == "unavailable"


@pytest.mark.parametrize(
    "key,enabled,status",
    [
        (None, True, "unconfigured"),
        ("replace-me", True, "unconfigured"),
        ("secret", False, "disabled"),
    ],
)
def test_unconfigured_disabled_analyzer_never_sends_request(key, enabled, status):
    def forbidden(request):
        pytest.fail("No external request is allowed")

    analyzer = DeepSeekAnalyzer(
        key, enabled=enabled, transport=httpx.MockTransport(forbidden)
    )
    assert analyzer.analyze({})["status"] == status


def test_context_allowlist_preserves_decimal_strings_and_redacts_sensitive_text():
    source = {
        "id": "private-account-id",
        "book": "private account",
        "phase": "attention",
        "request": {
            "expected_symbol": "TEST",
            "expected_quote": "USDT",
            "url": "https://private.invalid",
            "book": "account",
        },
        "balances": {
            "quote_available": "50.00000001",
            "base_available": "0.12345678",
            "cookie": "session-secret",
        },
        "_diagnostic_context": {
            "order": {
                "state": "submission_unknown",
                "before_balances": {"quote_available": "100"},
                "last_check_error": "Authorization: Bearer raw-private-key",
                "cookie": "session-secret",
            },
            "recent_events": [
                {
                    "kind": "order_check",
                    "reason": "Cookie: sessionid=cookie-secret",
                    "evidence": {
                        "raw_html": "session-secret",
                        "balances": {"base_available": "1"},
                    },
                }
            ],
        },
    }
    context = diagnostic_context(
        source,
        "timeout https://secret.invalid/?key=hook-secret sk-private-key",
        ("hook-secret",),
    )
    serialized = json.dumps(context, ensure_ascii=False)
    for secret in (
        "private-account-id",
        "session-secret",
        "cookie-secret",
        "raw-private-key",
        "hook-secret",
        "sk-private-key",
        "private.invalid",
    ):
        assert secret not in serialized
    assert context["balances"]["quote_available"] == "50.00000001"
    assert context["order"]["state"] == "submission_unknown"
    assert "真实" not in context["limitations"] or "没有" in context["limitations"]


def test_model_output_is_redacted():
    content = diagnosis()
    content["summary"] = "推测：sk-private-key；参考 https://private.invalid/?key=abc"
    analyzer = DeepSeekAnalyzer(
        "sk-private-key",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=completion(content))
        ),
    )
    result = analyzer.analyze({})
    assert result["status"] == "completed"
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "settings",
    [
        {"ALPHALOOPER_DEEPSEEK_TIMEOUT_SECONDS": "secret-invalid"},
        {"ALPHALOOPER_DEEPSEEK_TIMEOUT_SECONDS": "999"},
        {"ALPHALOOPER_DEEPSEEK_MODEL": "bad/model"},
        {"ALPHALOOPER_AI_DIAGNOSTICS_ENABLED": "maybe"},
    ],
)
def test_invalid_settings_do_not_break_startup(settings):
    analyzer = DeepSeekAnalyzer.from_env(settings.get)
    report = analyzer.analyze({})
    assert report["status"] == "unavailable" and "配置无效" in report["message"]
    assert "secret-invalid" not in json.dumps(report)


def test_quoted_cookie_and_bearer_are_redacted():
    result = safe_text(
        '"api_key": "some-private-key"\nCookie: sessionid=cookie-private\nBearer auth-private'
    )
    assert all(
        secret not in result
        for secret in ("some-private-key", "cookie-private", "auth-private")
    )


def test_manual_check_module_runs_without_network_when_disabled():
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {
        **os.environ,
        "ALPHALOOPER_AI_DIAGNOSTICS_ENABLED": "false",
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        [sys.executable, "-m", "scripts.check_ai_diagnostics"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        check=False,
        encoding="utf-8",
        timeout=5,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "disabled"
