import httpx
import pytest

from app import market


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(market, "ENV_FILE", path)
    monkeypatch.delenv("ALPHALOOPER_HTTP_PROXY", raising=False)
    monkeypatch.delenv("ALPHALOOPER_HTTP_TRANSPORT", raising=False)
    return path


def test_ordinary_start_loads_local_env_with_bom_and_quotes(
    env_file, tmp_path, monkeypatch
):
    env_file.write_text(
        'ALPHALOOPER_HTTP_PROXY="http://127.0.0.1:7897"\nALPHALOOPER_HTTP_TRANSPORT=curl\n',
        encoding="utf-8-sig",
    )
    monkeypatch.chdir(tmp_path.parent)
    assert market.network_settings() == ("http://127.0.0.1:7897", "curl")


def test_explicit_environment_overrides_file_including_direct(env_file, monkeypatch):
    env_file.write_text(
        "ALPHALOOPER_HTTP_PROXY=http://127.0.0.1:7897\nALPHALOOPER_HTTP_TRANSPORT=curl\n"
    )
    monkeypatch.setenv("ALPHALOOPER_HTTP_PROXY", "")
    monkeypatch.setenv("ALPHALOOPER_HTTP_TRANSPORT", "httpx")
    assert market.network_settings() == (None, "httpx")


def test_missing_env_keeps_direct_default(env_file):
    assert market.network_settings() == (None, "httpx")


def test_request_uses_file_proxy_and_error_does_not_expose_credentials(
    env_file, monkeypatch
):
    env_file.write_text("ALPHALOOPER_HTTP_PROXY=http://user:secret@127.0.0.1:7897\n")
    calls = []

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            raise httpx.ConnectError("private credential diagnostic")

    monkeypatch.setattr(market.httpx, "Client", Client)
    with pytest.raises(market.MarketError) as error:
        market.public_get(market.PREFIX + "klines")
    assert calls[0]["proxy"] == "http://user:secret@127.0.0.1:7897"
    assert "klines" in str(error.value) and "已配置代理" in str(error.value)
    assert "secret" not in str(error.value) and "private" not in str(error.value)
