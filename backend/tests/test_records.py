import json
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from sqlalchemy import select

from app import database, ledger, observations
from app.browser.records import read_records
from app.main import app

URL = "https://www.binance.com/zh-CN/alpha/bsc/0x1"


@pytest.fixture(scope="module")
def record_browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(record_browser):
    page = record_browser.new_page()
    page.route(
        "**/*",
        lambda route: route.fulfill(content_type="text/html", body="<html></html>"),
    )
    page.goto(URL)
    yield page
    page.close()


def test_read_only_keeps_text_ids_and_excludes_credentials(page):
    page.set_content("""<div role="tab" aria-selected="true">历史订单</div>
    <input type="password" value="secret-pass"><p>private unrelated profile text</p>
    <table><tr><th>订单编号</th><th>价格</th><th>状态</th></tr>
    <tr><td>000123456789012345678</td><td>0.00000001</td><td>已成交</td></tr></table>
    <table hidden><tr><th>订单编号</th></tr><tr><td>hidden secret</td></tr></table>
    <button onclick="window.actions++">撤单</button><button onclick="window.actions++">买入</button>
    <script>window.actions=0;localStorage.setItem('token','secret-token')</script>""")
    result = read_records(page, URL)
    assert result["status"] == "needs_mapping"
    assert result["verified_for_accounting"] is False
    assert result["tables"][0]["rows"][0][:2] == ["000123456789012345678", "0.00000001"]
    assert len(result["tables"]) == 1
    output = json.dumps(result)
    assert "secret" not in output and "private unrelated" not in output
    assert page.evaluate("window.actions") == 0


def test_login_and_empty_are_not_zero_trades(page):
    page.set_content('<button>登录</button><div role="tabpanel">暂无订单</div>')
    assert read_records(page, URL)["status"] == "login_required"
    page.set_content('<div role="tabpanel">暂无订单</div>')
    assert read_records(page, URL)["status"] == "empty_view_unverified"
    page.set_content("<div>行情</div>")
    assert read_records(page, URL)["status"] == "unsupported_view"


def test_identity_change_rejects_before_or_after_read():
    page = Mock()
    page.url = URL

    def navigate(_):
        page.url = URL + "2"
        return {}

    page.evaluate.side_effect = navigate
    with pytest.raises(ValueError, match="切换"):
        read_records(page, URL)
    page.evaluate.reset_mock()
    with pytest.raises(ValueError, match="不一致"):
        read_records(page, URL)
    page.evaluate.assert_not_called()


def test_large_tables_flag_incomplete(page):
    rows = "".join("<tr><td>" + ("x" * 600) + "</td></tr>" for _ in range(250))
    page.set_content("<table><tr><th>订单编号</th></tr>" + rows + "</table>")
    result = read_records(page, URL)
    assert result["truncated"] and len(result["tables"][0]["rows"]) == 200
    assert (
        sum(
            len(cell)
            for table in result["tables"]
            for row in table["rows"]
            for cell in row
        )
        <= 30000
    )
    assert not result["verified_for_accounting"]


def test_unrecognized_order_does_not_write_ledger_or_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "records.db")
    command.upgrade(Config("alembic.ini"), "head")
    with TestClient(app) as client:
        result = {
            "status": "unsupported_view",
            "captured_at": 123,
            "tables": [],
            "verified_for_accounting": False,
            "read_only": True,
        }
        execute = Mock(return_value={"ok": True, "observation": result})
        monkeypatch.setattr(app.state.browser, "execute", execute)
        assert (
            client.post("/api/account/orders/read", json={"url": URL}).status_code
            == 403
        )
        assert not execute.called
        headers = {"X-AlphaLooper-Client": "local-ui"}
        assert (
            client.post(
                "/api/account/orders/read",
                headers=headers,
                json={"url": "https://evil.example/alpha/bsc/0x1"},
            ).status_code
            == 422
        )
        assert not execute.called
        r = client.post("/api/account/orders/read", headers=headers, json={"url": URL})
        assert r.status_code == 200
        execute.assert_called_once_with("read_records", URL, None)
        assert r.json()["orders"] == []
        assert client.get("/api/browser/records").status_code == 404
        with app.state.engine.connect() as c:
            assert len(c.execute(select(observations.observations)).all()) == 0
            assert c.execute(select(ledger.fills)).all() == []
        assert app.state.browser._process is None


def test_expanded_detail_reads_id_only_without_truncation(page):
    from app.account_ledger import HEADERS, extract

    headers = "".join(f"<th>{h}</th>" for h in HEADERS)
    cells = [
        "2026-01-01 01:00:00",
        "TEST",
        "限价",
        "买入",
        "2 USDT",
        "2 USDT",
        "10 TEST",
        "10 TEST",
        "20 USDT",
        "-",
        "-",
        "-",
        "已成交",
    ]
    row = "".join(f"<td>{cell}</td>" for cell in cells)
    page.set_content(
        f"<table><tr>{headers}</tr></table><table><tr>{row}</tr>"
        "<tr><td>订单ID: 000123 更新时间: 2026-01-01 01:00:00 "
        + "逐笔成交不需要读取 " * 1000
        + "</td></tr></table>"
    )
    result = read_records(page, URL)
    assert not result["truncated"]
    assert result["tables"][1]["rows"][1] == ["订单ID: 000123"]
    parsed, skipped = extract(result)
    assert parsed[0]["order_id"] == "000123" and skipped == 0
    assert parsed[0]["gross"] == "20"


def test_order_api_upsert_and_reload(tmp_path, monkeypatch):
    from tests.test_account_ledger import observation

    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "orders.db")
    command.upgrade(Config("alembic.ini"), "head")
    with TestClient(app) as client:
        execute = Mock(return_value={"ok": True, "observation": observation()})
        monkeypatch.setattr(app.state.browser, "execute", execute)
        body = {"url": URL, "book": "fixture"}
        assert client.post("/api/account/orders/read", json=body).status_code == 403
        assert not execute.called
        for _ in range(2):
            response = client.post(
                "/api/account/orders/read",
                json=body,
                headers={"X-AlphaLooper-Client": "local-ui"},
            )
            assert response.status_code == 200
            assert len(response.json()["orders"]) == 1
        result = client.get("/api/account/orders?book=fixture").json()
        assert result["stats"][0]["buy_total"] == "20"
        assert result["stats"][0]["estimated_points"] == "80"
        assert execute.call_count == 2
        assert all(
            c.args == ("read_records", URL, None) for c in execute.call_args_list
        )
