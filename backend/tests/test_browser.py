import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.browser.manager import BrowserBusy, BrowserManager
from app.browser.schemas import OpenPage
from app.main import app


@pytest.mark.parametrize(
    "url",
    [
        "http://www.binance.com/en/alpha/bsc/token",
        "https://www.binance.com.evil.example/en/alpha",
        "https://www.binance.com@evil.example/en/alpha",
        "file:///C:/private/alpha",
        "https://www.binance.com:1234/en/alpha",
        "https://www.binance.com/en/trade",
    ],
)
def test_reject_untrusted_urls(url):
    with pytest.raises(ValidationError):
        OpenPage(url=url)


def test_alpha_url():
    assert OpenPage(url="https://www.binance.com/en/alpha/bsc/token").url.endswith(
        "token"
    )


def test_busy_rejects_second_operation():
    manager = BrowserManager()
    with manager._lock, pytest.raises(BrowserBusy):
        manager.execute("launch")


def test_status_does_not_launch_and_open_requires_launch():
    manager = BrowserManager()
    assert manager.execute("status")["connected"] is False
    assert manager.execute("open")["ok"] is False
    assert manager._process is None
    manager.close()


def test_foreign_origin_and_missing_header_cannot_launch():
    with TestClient(app) as client:
        assert client.post("/api/browser/launch").status_code == 403
        response = client.post(
            "/api/browser/launch",
            headers={
                "X-AlphaLooper-Client": "local-ui",
                "Origin": "https://evil.example",
            },
        )
        assert response.status_code == 403
        assert app.state.browser._process is None
        assert client.get("/api/browser/status").json()["connected"] is False


def test_invalid_url_does_not_start_worker():
    with TestClient(app) as client:
        response = client.post(
            "/api/browser/open",
            json={"url": "file:///alpha"},
            headers={"X-AlphaLooper-Client": "local-ui"},
        )
        assert response.status_code == 422
        assert app.state.browser._process is None
