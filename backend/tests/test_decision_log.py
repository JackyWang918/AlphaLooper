import json

from app import decision_log
from app.automatic import Automatic
from tests.test_automatic import rig, tick  # noqa: F401


def test_every_actual_evaluation_persists_complete_evidence_but_timer_does_not(rig):
    r = rig
    from decimal import Decimal as D

    r.market.asks = [(D("9.9"), D(100))]
    tick(r)
    first = decision_log.read(r.auto.engine, str(r.body.request_id), kind="buy_wait")[
        "items"
    ]
    assert len(first) == 1
    entry = first[0]
    assert len(entry["evidence"]["candles"]) == 3
    assert len(entry["evidence"]["estimate"]["buy_blockers"]) == 2
    assert entry["evidence"]["estimate"]["buy"] == "9.95"
    assert entry["evidence"]["config"]["buy_offset"] == "0.5"
    tick(r, 1)
    assert (
        len(
            decision_log.read(r.auto.engine, str(r.body.request_id), kind="buy_wait")[
                "items"
            ]
        )
        == 1
    )
    tick(r, 4)
    assert (
        len(
            decision_log.read(r.auto.engine, str(r.body.request_id), kind="buy_wait")[
                "items"
            ]
        )
        == 2
    )
    assert "live_submit" not in r.browser.calls


def test_fill_log_dedup_and_restart_reads_without_browser(rig):
    r = rig
    tick(r)
    r.browser.finish()
    tick(r, 60)
    fills = decision_log.read(r.auto.engine, str(r.body.request_id), kind="fill")[
        "items"
    ]
    assert len(fills) == 1 and fills[0]["details"]["result"]["order_id"] == "1"
    restarted = Automatic(r.auto.engine, r.live, r.auto.market, r.auto.clock)
    before_calls = list(r.browser.calls)
    assert restarted.get()["id"] == str(r.body.request_id)
    read_again = decision_log.read(r.auto.engine, str(r.body.request_id), kind="fill")[
        "items"
    ]
    assert read_again == fills and r.browser.calls == before_calls


def test_keyset_filter_export_and_task_isolation(rig):
    r = rig
    task_id = str(r.body.request_id)
    for i in range(6):
        with r.auto.engine.begin() as c:
            decision_log.append(
                c, task_id, "buy_wait", r.now[0], {"reason": f"判断{i}"}
            )
    with r.auto.engine.begin() as c:
        decision_log.append(
            c, "other-task", "buy_wait", r.now[0], {"reason": "不得混入"}
        )
    page1 = decision_log.read(r.auto.engine, task_id, kind="buy_wait", limit=2)
    # New logs after the first page must not cause duplicates in older pages.
    with r.auto.engine.begin() as c:
        decision_log.append(c, task_id, "buy_wait", r.now[0], {"reason": "新判断"})
    page2 = decision_log.read(
        r.auto.engine, task_id, before=page1["next_before"], kind="buy_wait", limit=2
    )
    assert not ({r["id"] for r in page1["items"]} & {r["id"] for r in page2["items"]})
    exported = [
        json.loads(row)
        for row in decision_log.export(r.auto.engine, task_id, "buy_wait")
    ]
    assert len(exported) == 7
    assert all(
        row["kind"] == "buy_wait" and row["reason"] != "不得混入" for row in exported
    )


def test_new_error_does_not_present_previous_market_as_fresh(rig):
    r = rig
    tick(r)
    r.browser.finish()

    def fail(*args):
        raise ValueError("公开行情请求失败")

    r.auto.market.snapshot = fail
    tick(r, 60)
    row = decision_log.read(r.auto.engine, str(r.body.request_id), kind="error")[
        "items"
    ][0]
    assert row["evidence"] == {} and "公开行情" in row["reason"]
