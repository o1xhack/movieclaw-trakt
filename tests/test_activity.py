import json

import httpx
import pytest

from trakt_sync.engine import Engine
from trakt_sync.policy import iso_time


async def test_account_overview_does_not_expose_credentials(harness):
    e, store, _, _ = await harness()
    account = await e.get(0, "account")
    await store.set("account", {**account, "username": "other-user"}, scope="member:3", secret=True)
    result = await e.accounts()
    assert [a["member_id"] for a in result["accounts"]] == [0, 3]
    assert result["accounts"][1]["username"] == "other-user"
    text = json.dumps(result)
    assert "fixture-access" not in text and "fixture-refresh" not in text
    assert "client_id" not in text and "generation" not in text


async def test_delivery_details_keep_title_episode_and_survive_restart(harness, event):
    e, store, clock, _ = await harness()
    event = {
        **event,
        "media": {"id": 8, "kind": "tv", "tmdb_id": 1435, "title": "节目 <test>"},
        "unit": {"season": 2, "episode": 3},
        "client": {"device_id": "private-device", "name": "private-name"},
    }
    await e.accept("completed", event, "episode", iso_time(clock[0]))
    before = (await e.activity(0))["records"][0]
    assert before["state"] == "queued" and before["title"] == "节目 <test>"
    await e.drain(0)
    restarted = Engine(store, e.client, e.health, e.log, clock=e.clock)
    row = (await restarted.activity(0))["records"][0]
    assert row["state"] == "sent" and row["season"] == 2 and row["episode"] == 3
    assert row["title"] == "节目 <test>" and row["completed_at"]
    for secret in ("fixture-access", "private-device", "private-name", "generation", "body"):
        assert secret not in json.dumps(row)
    assert (await e.activity(3))["records"] == []


@pytest.mark.parametrize(
    "code,state",
    [(404, "failed"), (422, "failed"), (429, "retrying"), (403, "blocked"), (503, "retrying")],
)
async def test_failures_show_exact_media_reason_and_retry_time(harness, event, code, state):
    e, _, clock, _ = await harness(lambda request: httpx.Response(code))
    await e.accept(
        "completed",
        {**event, "media": {**event["media"], "title": "测试影片"}},
        "fail",
        iso_time(clock[0]),
    )
    await e.drain(0)
    row = (await e.activity(0))["records"][0]
    assert row["state"] == state and row["title"] == "测试影片"
    assert row["error_status"] == code and row["reason"]
    assert row["can_retry"] == (state == "failed")
    assert bool(row["retry_at"]) == (state != "failed")


async def test_single_retry_does_not_retry_other_members_or_other_failures(harness, event):
    e, _, clock, _ = await harness(lambda request: httpx.Response(404))
    for i in range(2):
        await e.accept(
            "stopped",
            {**event, "client": {"device_id": f"device-{i}"}},
            f"failure-{i}",
            iso_time(clock[0]),
        )
        await e.drain(0)
    result = await e.retry_failed(0, "failure-0")
    assert result == {"retried": 1}
    ledger = await e.ledger(0)
    assert [j["id"] for j in ledger["queue"]] == ["failure-0"]
    assert [j["id"] for j in ledger["failed"]] == ["failure-1"]
    rows = {r["id"]: r for r in (await e.activity(0))["records"]}
    assert rows["failure-0"]["state"] == "queued"
    assert rows["failure-1"]["state"] == "failed"
    with pytest.raises(ValueError):
        await e.retry_failed(3, "failure-1")


async def test_coalesced_progress_is_not_shown_as_a_phantom_pending_job(harness, event):
    e, _, clock, _ = await harness()
    await e.accept("started", event, "start", iso_time(clock[0]))
    clock[0] += 61
    await e.accept("progress", event, "progress", iso_time(clock[0]))
    assert [r["id"] for r in (await e.activity(0))["records"]] == ["progress"]


async def test_legacy_queued_failures_remain_inspectable_without_title(harness, event):
    e, store, clock, _ = await harness(lambda request: httpx.Response(404))
    await e.accept("completed", event, "legacy", iso_time(clock[0]))
    await store.delete("activity", scope="member:0")
    await e.drain(0)
    row = (await e.activity(0))["records"][0]
    assert "TMDB 11" in row["title"] and row["state"] == "failed"


async def test_history_is_bounded_and_disconnect_removes_it(harness):
    e, store, clock, _ = await harness()
    for i in range(100):
        await e.record_activity(
            0, str(i), title="影" * 1000, state="sent", occurred_at=iso_time(clock[0])
        )
    rows = await e.get(0, "activity")
    assert len(rows) <= 80 and len(json.dumps(rows, ensure_ascii=False).encode()) <= 48000
    await e.disconnect(0)
    assert await e.get(0, "activity") is None
    assert not await store.scopes("account", entity="member")


async def test_activity_storage_failure_cannot_break_delivery(harness, event):
    e, store, clock, _ = await harness()
    original = store.set

    async def fail_only_log(key, value, **kwargs):
        if key == "activity":
            raise OSError("fixture")
        return await original(key, value, **kwargs)

    store.set = fail_only_log
    await e.accept("completed", event, "deliver", iso_time(clock[0]))
    await e.drain(0)
    assert not (await e.status(0))["pending"]
    assert (await e.status(0))["last_success"]


async def test_missing_media_id_is_visible_as_skipped_without_sending(harness, event):
    e, _, clock, requests = await harness()
    await e.accept(
        "started",
        {**event, "media": {"id": 1, "kind": "movie", "title": "没有编号"}},
        "skip",
        iso_time(clock[0]),
    )
    row = (await e.activity(0))["records"][0]
    assert row["state"] == "skipped" and "外部编号" in row["reason"]
    assert requests == []
