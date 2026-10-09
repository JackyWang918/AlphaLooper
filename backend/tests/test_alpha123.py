from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.alpha123 import Alpha123Client, Alpha123Error, parse_payload, parse_record


def record(**changes):
    value = {
        "token": "TEST",
        "chain_id": "56",
        "contract_address": "0xAbCd000000000000000000000000000000000001",
        "date": "2026-10-09",
        "time": "17:00",
        "type": "grab",
        "status": "announced",
    }
    value.update(changes)
    return value


def test_record_uses_beijing_time_and_chain_contract_identity():
    event = parse_record(record())

    assert event.contract_address == "0xabcd000000000000000000000000000000000001"
    assert event.scheduled_at == int(
        datetime(2026, 10, 9, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    )
    assert event.bsc_url.endswith(event.contract_address)
    assert event.event_id == parse_record(record(token="RENAMED")).event_id


@pytest.mark.parametrize(
    "changes",
    [
        {"contract_address": "0x1"},
        {"chain_id": "bsc"},
        {"time": "17:0"},
        {"date": "2026-22-09"},
    ],
)
def test_invalid_identity_or_time_never_becomes_event(changes):
    with pytest.raises(Alpha123Error):
        parse_record(record(**changes))


def test_payload_only_returns_grab_events_and_keeps_rejections_visible():
    events, rejected = parse_payload(
        {"airdrops": [record(), record(type="tge"), record(contract_address="bad")]}
    )

    assert [event.token for event in events] == ["TEST"]
    assert rejected == ["空投记录缺少有效链 ID 或 EVM 合约地址。"]


def test_client_uses_single_source_endpoint():
    seen = []
    client = Alpha123Client(lambda url: (seen.append(url) or {"airdrops": [record()]}))

    events, rejected = client.events()

    assert len(events) == 1 and rejected == []
    assert seen == ["https://alpha123.uk/api/data?fresh=1"]
