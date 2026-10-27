import json

import httpx
import pytest

from trakt_sync.policy import iso_time
from trakt_sync.transfers import Transfers


class Ops:
    def __init__(self, units):
        self.units, self.calls, self.writes = units, [], []
        self.versions = {}
        self.receipts = {}
        self.fail = False

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "playback.sync.catalog":
            after = tuple(map(int, args["cursor"].split(":"))) if args.get("cursor") else (0, 0, 0)
            found = [u for u in self.units if (u["media"]["id"], u["season"], u["episode"]) > after]
            rows = found[: args["limit"]]
            last = rows[-1] if rows else None
            return {
                "items": rows,
                "next_cursor": f"{last['media']['id']}:{last['season']}:{last['episode']}"
                if len(found) > len(rows)
                else None,
            }
        assert name == "playback.sync.import"
        outcomes = []
        for r in args["records"]:
            key = r["media_item_id"], r["season"], r["episode"]
            receipt = self.receipts.get(r["source_id"])
            if receipt:
                outcomes.append({"status": "already", "state_version": receipt})
            elif r["expected_updated_at"] != self.versions.get(key):
                outcomes.append({"status": "conflict", "state_version": None})
            else:
                stamp = iso_time(1609459200 + len(self.writes))
                self.versions[key] = self.receipts[r["source_id"]] = stamp
                self.writes.append(r)
                outcomes.append({"status": "applied", "state_version": stamp})
        if self.fail:
            self.fail = False
            raise OSError("response lost")
        return {"outcomes": outcomes}


def unit(i=1, kind="movie", season=0, episode=0, played=True, watches=1):
    return {
        "media": {"id": i, "kind": kind, "title": f"Movie {i}", "tmdb_id": i, "imdb_id": None},
        "season": season,
        "episode": episode,
        "played": played,
        "updated_at": None,
        "watches": [
            {
                "id": f"playback:{n}",
                "watched_at": f"2020-01-{n + 1:02}T00:00:00Z",
                "basis": "playback",
            }
            for n in range(watches)
        ],
    }


async def drive(t, task, state, clock=None):
    for _ in range(10000):
        task = await t.get(task["member_id"], task["id"])
        if task["state"] == state:
            return task
        assert task["state"] not in {"paused", "complete_with_errors", "cancelled"}, task
        if clock is not None:
            clock[0] = max(clock[0], task["next_at"] + 1)
        await t.step(task["id"], task["member_id"])
    raise AssertionError(task)


def remote(i, *, episode=None, when="2020-01-01T00:00:00Z", history_id=None):
    return {
        "id": history_id or i,
        "watched_at": when,
        "movie" if episode is None else "show": {"ids": {"tmdb": i}},
        **({"episode": {"season": 1, "number": episode}} if episode is not None else {}),
    }


async def test_foreground_confirm_grouping_export_incremental_and_rewatches(harness):
    sent = []

    def handler(r):
        if r.method == "POST" and r.url.path == "/sync/history":
            sent.append(json.loads(r.content))
            return httpx.Response(201, json={"added": {"movies": 1}})

    e, store, _, requests = await harness(handler)
    ops = Ops([unit(watches=3), unit(2, "tv", 1, 1), unit(2, "tv", 1, 2)])
    t = Transfers(e, ops)
    task = await t.create(0, "export")
    assert not requests and not ops.calls  # creating/listing does not scan or write
    await t.listing(0)
    task = await drive(t, task, "ready")
    assert (
        task["total"] == 5
        and task["movies"] == 1
        and task["episodes"] == 2
        and task["seasons"] == 1
    )
    details = await t.details(0, task["id"])
    assert len(details["items"]) == 2 and not sent
    with pytest.raises(ValueError):
        await t.confirm(0, task["id"], "wrong")
    await t.confirm(0, task["id"], task["preview_token"])
    assert not sent  # confirmation never schedules a worker
    t = Transfers(e, ops)  # resume after plugin reload, no implicit advancement
    assert (await t.get(0, task["id"]))["state"] == "running"
    task = await drive(t, task, "complete")
    assert task["done"] == 5 and len(sent) == 5
    assert [v["movies"][0]["watched_at"] for v in sent if "movies" in v] == [
        f"2020-01-0{i}T00:00:00Z" for i in (1, 2, 3)
    ]
    requests.clear()
    next_task = await drive(t, await t.create(0, "export"), "ready")
    assert next_task["total"] == 0 and not requests  # receipts skip both lookup and POST
    with pytest.raises(ValueError):
        await t.get(2, task["id"])


async def test_import_30000_history_records_pages_400_local_units_and_incremental(harness):
    records = [remote((i % 400) + 1, history_id=i + 1) for i in range(30000)]
    stamp = {"movies": {"watched_at": "2020-01-02T00:00:00Z"}}

    def handler(r):
        if r.url.path == "/sync/last_activities":
            return httpx.Response(200, json=stamp)
        if r.url.path == "/sync/history/movies":
            page = int(r.url.params["page"])
            return httpx.Response(
                200,
                json=records[(page - 1) * 100 : page * 100],
                headers={"X-Pagination-Page-Count": "300"},
            )

    e, _, clock, requests = await harness(handler)
    ops = Ops([unit(i, played=False) for i in range(1, 401)])
    t = Transfers(e, ops)
    task = await drive(t, await t.create(0, "import"), "ready", clock)
    assert task["total"] == 30000 and task["movies"] == 400
    assert len(ops.calls) == 8 and not ops.writes
    assert len([r for r in requests if r.url.path == "/sync/history/movies"]) == 300
    await t.confirm(0, task["id"], task["preview_token"])
    task = await drive(t, task, "complete", clock)
    assert task["done"] == 30000 and len(ops.writes) == 30000
    requests.clear()
    task = await drive(t, await t.create(0, "import"), "ready", clock)
    assert task["total"] == 0
    assert [r.url.path for r in requests] == ["/sync/last_activities"]


@pytest.mark.parametrize("kind", ["movie", "tv"])
@pytest.mark.parametrize("scan_delay", [0, 120])
async def test_import_reconsiders_post_cutoff_watches_with_unchanged_activity(
    harness, kind, scan_delay
):
    records = []
    stamp = {}
    path = "/sync/history/" + ("movies" if kind == "movie" else "episodes")

    def handler(r):
        if r.url.path == "/sync/last_activities":
            return httpx.Response(200, json=stamp)
        if r.url.path == path:
            return httpx.Response(200, json=records, headers={"X-Pagination-Page-Count": "1"})

    e, _, clock, requests = await harness(handler)
    ops = Ops([unit(kind=kind, season=int(kind == "tv"), episode=int(kind == "tv"))])
    t = Transfers(e, ops)
    task = await t.create(0, "import")
    # Either already future-dated, or added while the local scan is still in progress.
    eligible_at = clock[0] + 60
    when = iso_time(eligible_at)
    clock[0] += scan_delay
    records.append(remote(1, episode=1 if kind == "tv" else None, when=when))
    stamp["movies" if kind == "movie" else "episodes"] = {"watched_at": when}
    task = await drive(t, task, "ready", clock)
    assert task["total"] == 0 and not ops.writes
    await t.confirm(0, task["id"], task["preview_token"])
    await drive(t, task, "complete", clock)
    assert not await e.get(0, "import_stamp")

    clock[0] = max(clock[0], eligible_at + 1)
    requests.clear()
    task = await drive(Transfers(e, ops), await t.create(0, "import"), "ready", clock)
    assert task["total"] == 1 and any(r.url.path == path for r in requests)
    await t.confirm(0, task["id"], task["preview_token"])
    await drive(t, task, "complete", clock)
    assert len(ops.writes) == 1 and ops.writes[0]["watched_at"] == when
    assert await e.get(0, "import_stamp")

    requests.clear()
    task = await drive(t, await t.create(0, "import"), "ready", clock)
    assert task["total"] == 0
    assert [r.url.path for r in requests] == ["/sync/last_activities"]


async def test_import_lost_response_retries_idempotently_and_preserves_rewatch_versions(harness):
    def handler(r):
        if r.url.path == "/sync/last_activities":
            return httpx.Response(200, json={"movies": {"watched_at": "2020-01-02T00:00:00Z"}})
        if r.url.path == "/sync/history/movies":
            return httpx.Response(
                200,
                json=[remote(1, history_id=i, when=f"2020-01-0{i}T00:00:00Z") for i in (1, 2, 3)],
                headers={"X-Pagination-Page-Count": "1"},
            )

    e, _, clock, _ = await harness(handler)
    ops = Ops([unit(played=False)])
    t = Transfers(e, ops)
    task = await drive(t, await t.create(0, "import"), "ready", clock)
    await t.confirm(0, task["id"], task["preview_token"])
    ops.fail = True
    await t.step(task["id"], 0)
    assert len(ops.writes) == 1
    task = await drive(Transfers(e, ops), task, "complete", clock)
    assert task["done"] == 3 and len(ops.writes) == 3


async def test_export_uncertain_503_remote_receipt_deduplicates(harness):
    written = []

    def handler(r):
        if r.method == "POST":
            written.append(json.loads(r.content)["movies"][0])
            return httpx.Response(503)
        if r.url.path == "/sync/history/movies":
            return httpx.Response(200, json=[remote(1, when=w["watched_at"]) for w in written])

    e, _, clock, _ = await harness(handler)
    t = Transfers(e, Ops([unit()]))
    task = await drive(t, await t.create(0, "export"), "ready")
    await t.confirm(0, task["id"], task["preview_token"])
    task = await drive(t, task, "complete", clock)
    assert task["done"] == 1 and len(written) == 1


async def test_conflicts_unwatch_after_scan_are_not_reported_complete(harness):
    e, _, _, requests = await harness()
    ops = Ops([unit()])
    t = Transfers(e, ops)
    task = await drive(t, await t.create(0, "export"), "ready")
    ops.units[0]["played"] = False
    await t.confirm(0, task["id"], task["preview_token"])
    await t.step(task["id"], 0)
    task = await t.step(task["id"], 0)
    assert task["state"] == "complete_with_errors" and task["skipped"] == 1 and not requests


async def test_retry_429_pause_cancel_and_account_generation(harness):
    fail = [True]

    def handler(r):
        if fail[0]:
            return httpx.Response(429, headers={"Retry-After": "120"})

    e, _, clock, requests = await harness(handler)
    t = Transfers(e, Ops([unit()]))
    task = await drive(t, await t.create(0, "import"), "scanning")
    await t.step(task["id"], 0)
    delayed = await t.get(0, task["id"])
    assert delayed["next_at"] >= clock[0] + 120
    await t.step(task["id"], 0)
    assert len(requests) == 1
    await t.control(0, task["id"], "pause")
    await t.step(task["id"], 0)
    assert len(requests) == 1
    fail[0] = False
    await t.control(0, task["id"], "resume")
    await e.set(0, "account", {**await e.get(0, "account"), "generation": "changed"}, secret=True)
    assert (await t.step(task["id"], 0))["state"] == "cancelled"


async def test_repeated_remote_page_and_changed_preview_require_rescan(harness):
    changed = [False]
    repeat = [True]

    def handler(r):
        if r.url.path == "/sync/last_activities":
            return httpx.Response(
                200,
                json={
                    "movies": {
                        "watched_at": "2020-01-03T00:00:00Z"
                        if changed[0]
                        else "2020-01-02T00:00:00Z"
                    }
                },
            )
        if r.url.path == "/sync/history/movies":
            return httpx.Response(
                200,
                json=[remote(1)],
                headers={"X-Pagination-Page-Count": "2" if repeat[0] else "1"},
            )

    e, _, clock, _ = await harness(handler)
    t = Transfers(e, Ops([unit()]))
    task = await t.create(0, "import")
    for _ in range(3):
        task = await t.step(task["id"], 0)
    assert task["state"] == "paused" and "重复分页" in task["error"]
    await t.control(0, task["id"], "cancel")
    repeat[0] = False
    task = await drive(t, await t.create(0, "import"), "ready", clock)
    changed[0] = True
    with pytest.raises(ValueError, match="历史已变化"):
        await t.confirm(0, task["id"], task["preview_token"])
    assert not t.ops.writes


async def test_import_state_conflict_is_reported_and_not_cached_as_fully_synced(harness):
    def handler(r):
        if r.url.path == "/sync/last_activities":
            return httpx.Response(200, json={"movies": {"watched_at": "2020-01-02T00:00:00Z"}})
        if r.url.path == "/sync/history/movies":
            return httpx.Response(200, json=[remote(1)], headers={"X-Pagination-Page-Count": "1"})

    e, _, clock, _ = await harness(handler)
    ops = Ops([unit(played=False)])
    t = Transfers(e, ops)
    task = await drive(t, await t.create(0, "import"), "ready", clock)
    ops.versions[1, 0, 0] = "2026-01-01T00:00:00Z"
    await t.confirm(0, task["id"], task["preview_token"])
    await t.step(task["id"], 0)
    task = await t.step(task["id"], 0)
    assert task["state"] == "complete_with_errors" and task["skipped"] == 1 and not ops.writes
    assert not await e.get(0, "import_stamp")
    assert (await t.details(0, task["id"], records=True))["items"][0]["reason"] == "conflict"


async def test_scan_storage_failure_replays_page_without_duplicate_plan_rows(harness):
    e, store, clock, _ = await harness()
    t = Transfers(e, Ops([unit(i) for i in range(1, 42)]))
    task = await t.create(0, "export")
    original = store.set
    failed = [False]

    async def flaky(key, value, **kwargs):
        if key == "plan_1" and not failed[0]:
            failed[0] = True
            raise OSError("fixture disk interruption")
        return await original(key, value, **kwargs)

    store.set = flaky
    await t.step(task["id"], 0)
    task = await drive(t, task, "ready", clock)
    rows = (await t.details(0, task["id"], records=True))["items"]
    rows += (await t.details(0, task["id"], page=1, records=True))["items"]
    assert task["total"] == 41 and len(rows) == 41 and len({r["media"]["id"] for r in rows}) == 41


async def test_reconnecting_same_account_keeps_fresh_preview_version(harness):
    count = [3]

    def handler(r):
        if r.url.path == "/sync/last_activities":
            return httpx.Response(
                200, json={"movies": {"watched_at": f"2020-01-0{count[0]}T00:00:00Z"}}
            )
        if r.url.path == "/sync/history/movies":
            return httpx.Response(
                200,
                json=[
                    remote(1, history_id=i, when=f"2020-01-0{i}T00:00:00Z")
                    for i in range(1, count[0] + 1)
                ],
                headers={"X-Pagination-Page-Count": "1"},
            )

    e, _, clock, _ = await harness(handler)
    await e.set(0, "account", {**await e.get(0, "account"), "username": "same-user"}, secret=True)
    ops = Ops([unit(played=False)])
    t = Transfers(e, ops)
    task = await drive(t, await t.create(0, "import"), "ready", clock)
    await t.confirm(0, task["id"], task["preview_token"])
    await drive(t, task, "complete", clock)
    await e.set(
        0, "account", {**await e.get(0, "account"), "generation": "reconnected"}, secret=True
    )
    ops.units[0]["updated_at"] = ops.versions[1, 0, 0]
    count[0] = 4
    task = await drive(t, await t.create(0, "import"), "ready", clock)
    await t.confirm(0, task["id"], task["preview_token"])
    task = await drive(t, task, "complete", clock)
    assert len(ops.writes) == 4 and task["done"] == 4


@pytest.mark.parametrize("oauth", [False, True])
async def test_trakt_auth_failure_blocks_account_and_pauses_other_transfer(harness, oauth):
    from trakt_sync.client import TraktError

    e, _, _, _ = await harness()
    ops = Ops([unit()])
    t = Transfers(e, ops)
    task = await t.create(0, "export")

    async def rejected(*args, **kwargs):
        raise TraktError(400 if oauth else 403, auth_error=oauth)

    t.request = rejected
    task = await t.get(0, task["id"])
    task["phase"] = "remote"
    await t.save(task)
    # An import scan uses the same authenticated request path.
    task["direction"] = "import"
    await t.save(task)
    result = await t.step(task["id"], 0)
    assert result["state"] == "paused"
    assert (await e.status(0))["needs_reconnect"]
    await t.control(0, task["id"], "resume")
    result = await t.step(task["id"], 0)
    assert result["state"] == "paused" and "Trakt" in result["error"]


async def test_host_permission_failure_does_not_revoke_trakt_account(harness):
    from movieclaw_sdk.ops import OpsError

    e, _, _, requests = await harness()
    ops = Ops([unit()])

    async def forbidden(*args, **kwargs):
        raise OpsError(403, "FORBIDDEN", "fixture missing host grant")

    ops.call = forbidden
    t = Transfers(e, ops)
    task = await t.create(0, "export")
    result = await t.step(task["id"], 0)
    assert result["state"] == "paused" and "操作授权" in result["error"]
    assert (await e.status(0))["connected"] and not requests


async def test_export_item_400_does_not_abort_other_rows(harness):
    def handler(r):
        if r.method == "POST" and json.loads(r.content)["movies"][0]["ids"]["tmdb"] == 1:
            return httpx.Response(400)
        if r.method == "POST":
            return httpx.Response(201, json={"added": {"movies": 1}})

    e, _, _, _ = await harness(handler)
    t = Transfers(e, Ops([unit(), unit(2)]))
    task = await drive(t, await t.create(0, "export"), "ready")
    await t.confirm(0, task["id"], task["preview_token"])
    await t.step(task["id"], 0)
    result = await t.step(task["id"], 0)
    assert result["state"] == "complete_with_errors"
    assert result["done"] == 1 and result["failed"] == 1
    assert (await e.status(0))["connected"]


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_confirmation_remote_failure_retains_preview_and_honors_retry_after(harness, status):
    rejected = [False]

    def handler(r):
        if r.url.path == "/sync/last_activities":
            if rejected[0]:
                return httpx.Response(status, headers={"Retry-After": "120"})
            return httpx.Response(200, json={"movies": {"watched_at": "2020-01-01T00:00:00Z"}})
        if r.url.path == "/sync/history/movies":
            return httpx.Response(200, json=[remote(1)], headers={"X-Pagination-Page-Count": "1"})

    e, _, clock, requests = await harness(handler)
    ops = Ops([unit(played=False)])
    t = Transfers(e, ops)
    task = await drive(t, await t.create(0, "import"), "ready")
    rejected[0] = True
    with pytest.raises(ValueError):
        await t.confirm(0, task["id"], task["preview_token"])
    retained = await t.get(0, task["id"])
    assert not retained.get("confirmed_at") and not ops.writes
    assert retained["preview_token"] == task["preview_token"]
    assert (await e.status(0))["needs_reconnect"] == (status == 403)
    if status == 403:
        assert retained["state"] == "ready"
    else:
        assert retained["state"] == "ready" and retained["next_at"] >= clock[0] + 120
        count = len(requests)
        with pytest.raises(ValueError):
            await t.confirm(0, task["id"], task["preview_token"])
        assert len(requests) == count
        rejected[0] = False
        clock[0] = retained["next_at"] + 1
        assert (await t.confirm(0, task["id"], task["preview_token"]))["state"] == "running"
        assert not ops.writes
