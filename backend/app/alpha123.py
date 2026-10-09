"""Read and normalize Alpha123 airdrop notices; this module never trades."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

SOURCE_URL = "https://alpha123.uk/api/data?fresh=1"
BEIJING = ZoneInfo("Asia/Shanghai")
EVM_ADDRESS = re.compile(r"0x[a-fA-F0-9]{40}\Z")
TIME = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")


class Alpha123Error(ValueError):
    pass


@dataclass(frozen=True)
class AirdropEvent:
    """One source event identified by its scheduled time and on-chain identity."""

    event_id: str
    token: str
    chain_id: str
    contract_address: str
    scheduled_at: int
    source_status: str
    source_type: str

    @property
    def bsc_url(self) -> str:
        if self.chain_id != "56":
            raise Alpha123Error("当前仅支持把 BSC 空投记录转换为 Alpha 交易链接。")
        return f"https://www.binance.com/zh-CN/alpha/bsc/{self.contract_address}"


def parse_record(record: object) -> AirdropEvent:
    if not isinstance(record, dict):
        raise Alpha123Error("空投记录不是对象。")
    token = str(record.get("token", "")).strip()
    chain_id = str(record.get("chain_id", "")).strip()
    address = str(record.get("contract_address", "")).strip().lower()
    date = str(record.get("date", "")).strip()
    clock = str(record.get("time", "")).strip()
    if not token or len(token) > 30:
        raise Alpha123Error("空投记录缺少有效 token 符号。")
    if not chain_id.isdecimal() or not EVM_ADDRESS.fullmatch(address):
        raise Alpha123Error("空投记录缺少有效链 ID 或 EVM 合约地址。")
    if not TIME.fullmatch(clock):
        raise Alpha123Error("空投记录缺少精确到分钟的北京时间。")
    try:
        scheduled = datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M").replace(
            tzinfo=BEIJING
        )
    except ValueError as exc:
        raise Alpha123Error("空投记录日期无效。") from exc
    # The source can correct names/statuses. The scheduled on-chain identity is
    # deliberately the durable de-duplication key instead of the ticker symbol.
    key = f"{scheduled.isoformat()}|{chain_id}|{address}"
    return AirdropEvent(
        event_id=hashlib.sha256(key.encode()).hexdigest(),
        token=token,
        chain_id=chain_id,
        contract_address=address,
        scheduled_at=int(scheduled.timestamp()),
        source_status=str(record.get("status", "")).strip(),
        source_type=str(record.get("type", "")).strip(),
    )


def parse_payload(payload: object) -> tuple[list[AirdropEvent], list[str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("airdrops"), list):
        raise Alpha123Error("Alpha123 响应缺少 airdrops 列表。")
    events, rejected = [], []
    for raw in payload["airdrops"]:
        try:
            event = parse_record(raw)
        except Alpha123Error as exc:
            rejected.append(str(exc))
            continue
        # Airdrop entries use grab in the observed source. Other rows, such as
        # TGE information, are intentionally not eligible for the entry flow.
        if event.source_type != "grab":
            continue
        events.append(event)
    return events, rejected


class Alpha123Client:
    def __init__(self, get=None):
        self.get = get or self._get

    @staticmethod
    def _get(url: str):
        with httpx.Client(timeout=8, trust_env=False) as client:
            response = client.get(url, headers={"Cache-Control": "no-cache"})
            response.raise_for_status()
            return response.json()

    def events(self) -> tuple[list[AirdropEvent], list[str]]:
        try:
            payload = self.get(SOURCE_URL)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise Alpha123Error("无法读取 Alpha123 空投数据。") from exc
        return parse_payload(payload)
