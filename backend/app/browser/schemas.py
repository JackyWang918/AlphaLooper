import re
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


class OpenPage(BaseModel):
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str):
        value = value.strip()
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.hostname != "www.binance.com"
            or url.username
            or url.password
            or url.port not in (None, 443)
        ):
            raise ValueError("请输入 https://www.binance.com/ 开头的 Alpha 页面链接。")
        if "/alpha" not in url.path.lower():
            raise ValueError("请输入 Alpha 页面链接。")
        return value


def token_identity(value: str):
    parsed = urlsplit(OpenPage(url=value).url)
    match = re.fullmatch(
        r"/(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?alpha/([^/]+)/([^/]+)/?", parsed.path
    )
    if not match:
        raise ValueError("链接必须指向具体 Alpha 币种。")
    chain, address = match.groups()
    return chain.lower(), address.lower() if chain.lower() == "bsc" else address


class FillForm(BaseModel):
    url: str
    side: Literal["buy", "sell"]
    price: str = Field(max_length=40)
    quantity: str = Field(max_length=40)
    expected_symbol: str = Field(min_length=1, max_length=30)
    expected_quote: str = Field(min_length=1, max_length=30)

    @field_validator("url")
    @classmethod
    def concrete_token(cls, value):
        token_identity(value)
        return value.strip()

    @field_validator("price", "quantity")
    @classmethod
    def positive_decimal(cls, value):
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) or Decimal(value) <= 0:
            raise ValueError("请输入大于零的普通十进制数，不支持科学计数法。")
        return value


class ReadRecords(BaseModel):
    book: str = Field(default="本机账户", min_length=1, max_length=80, pattern=r".*\S.*")
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def concrete_token(cls, value):
        token_identity(value)
        return value.strip()
