import asyncio
from collections import Counter

import httpx
import pytest

from trakt_sync.background import BackgroundImport
from trakt_sync.policy import identity, iso_time, timestamp
from trakt_sync.preferences import preferences


def episode(record, number, when, *, series=200, trakt=100):
    return {
        "id": record,
        "watched_at": when,
        "show": {"ids": {"tmdb": series, "trakt": 800}},
        "episode": {"season": 1, "number": number, "ids": {"trakt": trakt + number}},
    }


def local_episode(number, unit_version=None, **values):
    return {
        "media": {"id": 70, "kind": "tv", "title": "Series", "tmdb_id": 200},
        "season": 1,
        "episode": number,
        "updated_at": unit_version,
        "played": False,
        "watches": [],
        **values,
    }


class Host:
    def __init__(self, units):
        self.units = {u["episode"]: u for u in units}
        self.records = {}
        self.calls = Counter()
        self.member_created = "2020-01-01T00:00:00Z"
        self.conflict = False

    async def call(self, op, body):
        self.calls[op] += 1
        if op == "members.show":
            return {"id": body["member_id"], "status": "active", "created_at": self.member_created}
        if op == "playback.sync.catalog":
            return {"items": list(self.units.values()), "next_cursor": None}
        assert op == "playback.sync.import"
        outcomes = []
        for row in body["records"]:
            unit = self.units[row["episode"]]
            key = body["member_id"], row["source_id"]
            if key in self.records:
                outcome = {"status": "already"}
            elif self.conflict or row["expected_updated_at"] != unit["updated_at"]:
                outcome = {"status": "conflict"}
            else:
                self.records[key] = row
                outcome = {
                    "status": "applied",
                    "state_version": iso_time(1700000000 + len(self.records)),
                }
            outcomes.append(outcome)
        for row, outcome in zip(body["records"], outcomes, strict=True):
            if outcome["status"] == "applied":
                self.units[row["episode"]].update(played=True, updated_at=outcome["state_version"])
        return {"outcomes": outcomes}


async def opt_in(engine, worker, member=0):
    account = await engine.get(member, "account")
    await engine.set(
        member,
        "automatic_import_binding",
        {
            "generation": account["generation"],
            "member": await worker.member_identity(member),
        },
    )


async def test_late_episode_backdated_updates_and_restart(harness):
    stamp = ["2026-01-01T00:00:00Z"]
    rows = [episode(1, 1, "2020-01-01T11:00:00Z")]

    def trakt(request):
        path = request.url.path
        if path == "/sync/last_activities":
            return httpx.Response(
                200,
                json={
                    "movies": {"watched_at": "1970-01-01T00:00:00Z"},
                    "episodes": {"watched_at": stamp[0]},
                },
            )
        if path == "/sync/watched/shows":
            assert request.url.params["extended"] == "noseasons"
            return httpx.Response(
                200, json=[{"show": {"ids": {"trakt": 800}}, "last_updated_at": stamp[0]}]
            )
        if path == "/sync/watched/movies":
            return httpx.Response(200, json=[])
        if path in {"/sync/history/episodes", "/sync/history/shows/800"}:
            return httpx.Response(200, json=rows, headers={"X-Pagination-Page-Count": "1"})
        return None

    engine, store, clock, requests = await harness(trakt)
    host = Host([local_episode(5)])
    worker = BackgroundImport(engine, host)
    await worker.process(0)
    assert not requests and not host.calls

    await opt_in(engine, worker)
    await worker.process(0)
    assert not host.records
    assert "/sync/history/episodes" in [r.url.path for r in requests]
    requests.clear()
    await worker.process(0)
    assert [r.url.path for r in requests] == ["/sync/last_activities"]

    host.units.update({n: local_episode(n) for n in (1, 2, 3, 4)})
    worker = BackgroundImport(engine, host)
    requests.clear()
    await worker.process(0)
    assert [r["episode"] for r in host.records.values()] == [1]
    assert next(iter(host.records.values()))["watched_at"] == "2020-01-01T11:00:00Z"
    assert not any(r.url.path.startswith("/sync/history/") for r in requests)

    # A newly entered play can have a date earlier than every previous reconciliation.
    rows.append(episode(2, 2, "2019-01-01T11:00:00Z"))
    stamp[0] = "2026-01-02T00:00:00Z"
    requests.clear()
    await worker.process(0)
    assert [r["episode"] for r in host.records.values()] == [1, 2]
    assert "/sync/history/shows/800" in [r.url.path for r in requests]
    assert not any(r.method == "POST" for r in requests)
    assert host.units[3]["played"] is False

    # Restart and library checks never replay a completed Trakt record.
    host.units[1].update(played=False, updated_at=iso_time(clock[0]))
    requests.clear()
    imports = host.calls["playback.sync.import"]
    clock[0] += 8 * 86400
    await engine.set(
        0, "account", {**(await engine.get(0, "account")), "expires_at": clock[0] + 3600}
    )
    worker = BackgroundImport(engine, host)
    await worker.process(0)
    assert [r.url.path for r in requests] == ["/sync/last_activities"]
    assert host.calls["playback.sync.import"] == imports
    assert host.units[1]["played"] is False
    assert len(host.records) == 2
    assert (await engine.get(0, "background_import"))["imported"] == 2
    assert len(await store.scopes("task", entity="history")) == 0


async def test_conflict_backoff_local_unwatch_and_account_bound_opt_in(harness):
    stamp = "2026-01-01T00:00:00Z"
    rows = [episode(1, 1, "2020-01-01T00:00:00Z"), episode(2, 2, "2020-02-01T00:00:00Z")]

    def trakt(request):
        if request.url.path == "/sync/last_activities":
            return httpx.Response(200, json={"episodes": {"watched_at": stamp}})
        if request.url.path == "/sync/history/episodes":
            return httpx.Response(200, json=rows, headers={"X-Pagination-Page-Count": "1"})
        return None

    engine, _, clock, requests = await harness(trakt)
    await engine.set(2, "account", {**(await engine.get(0, "account")), "generation": "member-2"})
    host = Host([local_episode(1), local_episode(2)])
    worker = BackgroundImport(engine, host)
    await opt_in(engine, worker, 2)
    await worker.remember_unwatch(
        {
            "member_id": 2,
            "media": host.units[1]["media"],
            "units": [{"season": 1, "episode": 1}],
        },
        "2025-01-01T00:00:00Z",
    )
    host.conflict = True
    await worker.process(2)
    assert not host.records
    retry = await engine.get(2, "background_import")
    assert retry["stamp"]["episodes"] == stamp and retry["retry_at"] > clock[0]
    before = len(requests)
    await BackgroundImport(engine, host).process(2)
    assert len(requests) == before
    clock[0] = retry["retry_at"]
    host.conflict = False
    await worker.process(2)
    assert [r["episode"] for r in host.records.values()] == [2]
    assert (await worker.status(2))["protected"] == 1

    before = len(requests)
    host.member_created = "2026-01-01T00:00:00Z"
    await worker.process(2)
    assert len(requests) == before
    assert "成员已更换" in (await worker.status(2))["error"]
    await engine.set(2, "account", {**(await engine.get(2, "account")), "generation": "other"})
    await worker.process(2)
    assert len(requests) == before


async def test_scheduler_reacts_to_inventory_and_stops_on_cancel(harness):
    def trakt(request):
        if request.url.path == "/sync/last_activities":
            return httpx.Response(200, json={"movies": {"watched_at": "2026-01-01T00:00:00Z"}})
        return httpx.Response(200, json=[])

    engine, _, _, _ = await harness(trakt)
    host = Host([])
    worker = BackgroundImport(engine, host)
    await opt_in(engine, worker)
    await engine.set(
        0,
        "account",
        {
            **(await engine.get(0, "account")),
            "user_uuid": "fixture",
            "identity_version": 1,
        },
    )

    async def wait_until_idle(expected):
        async with asyncio.timeout(2):
            while 0 in worker.active or host.calls["playback.sync.catalog"] < expected:
                await asyncio.sleep(0.01)

    task = asyncio.create_task(worker.run())
    try:
        await wait_until_idle(1)
        worker.inventory_changed()
        async with asyncio.timeout(2):
            while worker.catalog_revision.get(0) != worker.library_revision or worker.active:
                await asyncio.sleep(0.01)
        assert host.calls["playback.sync.catalog"] == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not worker.active


async def test_retry_after_and_disable_before_write(harness):
    throttle = [True]

    def trakt(request):
        if request.url.path == "/sync/last_activities":
            if throttle[0]:
                return httpx.Response(429, headers={"Retry-After": "600"})
            return httpx.Response(200, json={"episodes": {"watched_at": "2026-01-01T00:00:00Z"}})
        if request.url.path == "/sync/history/episodes":
            return httpx.Response(
                200,
                json=[episode(1, 1, "2020-01-01T00:00:00Z")],
                headers={"X-Pagination-Page-Count": "1"},
            )
        return None

    scanning, resume = asyncio.Event(), asyncio.Event()

    class PausableHost(Host):
        async def call(self, op, payload):
            if op == "playback.sync.catalog":
                scanning.set()
                await resume.wait()
            return await super().call(op, payload)

    engine, store, clock, requests = await harness(trakt)
    host = PausableHost([local_episode(1)])
    worker = BackgroundImport(engine, host)
    await opt_in(engine, worker)
    await worker.process(0)
    assert (await worker.status(0))["retry_at"] == clock[0] + 600
    throttle[0] = False
    clock[0] += 599
    await BackgroundImport(engine, host).process(0, requested=True)
    assert len(requests) == 1 and not scanning.is_set()

    clock[0] += 1
    task = asyncio.create_task(BackgroundImport(engine, host).process(0))
    try:
        await asyncio.wait_for(scanning.wait(), timeout=2)
        async with engine.locks[0]:
            await store.delete("automatic_import_binding", scope="member:0")
    finally:
        resume.set()
        await task
    assert not host.records
    assert host.calls["playback.sync.import"] == 0


async def test_explicit_unwatch_before_connection_outweighs_later_favorite(harness):
    engine, store, _, _ = await harness()
    account = await engine.get(0, "account")
    await store.delete("account", scope="member:0")
    await store.delete("linked", scope="member:0")
    unit = local_episode(
        1,
        "2025-01-03T00:00:00Z",
        last_played_at=None,
        position_ms=0,
    )
    worker = BackgroundImport(engine, Host([unit]))
    await worker.remember_unwatch(
        {"member_id": 0, "media": unit["media"], "units": [{"season": 1, "episode": 1}]},
        "2025-01-01T00:00:00Z",
    )
    await engine.set(0, "account", account, secret=True)
    await opt_in(engine, worker)
    item = identity({"media": unit["media"], "unit": unit})
    assert await worker.protected(0, "admin", unit, item, "2024-12-31T00:00:00Z")
    unit["watches"] = [{"basis": "import", "watched_at": "2024-12-31T00:00:00Z"}]
    assert not await worker.protected(0, "admin", unit, item, "2025-01-02T00:00:00Z")


async def test_catalog_removal_reconciles_surviving_previously_ambiguous_episode(harness):
    row = episode(1, 1, "2020-01-01T11:00:00Z")

    def trakt(request):
        if request.url.path == "/sync/last_activities":
            return httpx.Response(200, json={"episodes": {"watched_at": "2020-01-01T11:00:00Z"}})
        if request.url.path == "/sync/history/episodes":
            return httpx.Response(200, json=[row], headers={"X-Pagination-Page-Count": "1"})
        return None

    engine, _, _, requests = await harness(trakt)
    first, duplicate = local_episode(1), local_episode(1)
    duplicate["media"]["id"] = 71
    host = Host([first])
    host.units[2] = duplicate
    worker = BackgroundImport(engine, host)
    await opt_in(engine, worker)
    await worker.process(0)
    assert not host.records

    del host.units[2]
    worker.inventory_changed()
    requests.clear()
    await worker.process(0)
    assert [r["episode"] for r in host.records.values()] == [1]
    assert not any(r.url.path.startswith("/sync/history/") for r in requests)


@pytest.mark.parametrize("lost_response", [False, True])
async def test_outbound_native_id_skips_cross_minute_echo_but_keeps_rewatch(harness, lost_response):
    native_at = "2026-01-01T11:00:59Z"
    echoed_at = "2026-01-01T11:01:00Z"
    rewatch_at = "2026-01-02T11:01:00Z"
    rows = []

    def trakt(request):
        if request.url.path == "/scrobble/stop":
            rows.append({**episode(123, 1, echoed_at), "action": "scrobble"})
            if lost_response:
                raise httpx.ReadError("fixture", request=request)
            return httpx.Response(201, json={"action": "scrobble", "id": 123})
        if request.url.path == "/sync/last_activities":
            return httpx.Response(
                200, json={"episodes": {"watched_at": rewatch_at if len(rows) > 1 else echoed_at}}
            )
        if request.url.path == "/sync/history/episodes":
            return httpx.Response(200, json=rows, headers={"X-Pagination-Page-Count": "1"})
        return None

    engine, _, clock, _ = await harness(trakt)
    unit = local_episode(1)
    unit.update(played=True, watches=[{"basis": "playback", "watched_at": native_at}])
    clock[0] = timestamp("2026-01-01T11:01:01Z")
    await engine.accept(
        "completed",
        {
            "member_id": 0,
            "media": unit["media"],
            "unit": unit,
            "position_ms": 100,
            "duration_ms": 100,
            "watch_source_ids": ["source-1"],
        },
        "native",
        native_at,
    )
    await engine.drain(0)
    host = Host([unit])
    worker = BackgroundImport(engine, host)
    await opt_in(engine, worker)
    if lost_response:
        await worker.process(0)
        assert not host.records
        assert (await engine.get(0, "background_import"))["pending_retry"]
        clock[0] += 70
        await engine.drain(0)
    account = await engine.get(0, "account")
    assert await engine.outbound_scrobble(
        0, account, 123, identity({"media": unit["media"], "unit": unit})
    )
    clock[0] = timestamp("2026-01-03T00:00:00Z")
    rows.append({**episode(124, 1, rewatch_at), "action": "scrobble"})
    await worker.process(0)
    assert [r["watched_at"] for r in host.records.values()] == [rewatch_at]


async def test_lost_scrobble_does_not_exclude_any_ambiguous_history_ids(harness):
    rows = [
        {**episode(321, 1, "2026-01-01T11:00:00Z"), "action": "scrobble"},
        {**episode(322, 1, "2026-01-01T11:01:00Z"), "action": "scrobble"},
    ]

    def trakt(request):
        assert request.url.path == "/sync/history/episodes"
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[rows[page - 1]], headers={"X-Pagination-Page-Count": "2"})

    engine, _, _, requests = await harness(trakt)
    account = await engine.get(0, "account")
    unit = local_episode(1)
    item = identity({"media": unit["media"], "unit": unit})
    found, received_id = await engine.find_delivered_history(
        0,
        account,
        {
            "item": item,
            "occurred_at": "2026-01-01T10:59:59Z",
            "scrobble_window": {
                "item": item,
                "start": "2026-01-01T11:00:59Z",
                "end": "2026-01-01T11:01:01Z",
            },
        },
    )
    assert found and received_id is None
    assert len(requests) == 2


async def test_stale_binding_requires_new_consent_after_reconnection(harness):
    engine, _, _, _ = await harness()
    await opt_in(engine, BackgroundImport(engine, Host([])))
    assert (await preferences(engine, 0)).automatic_import
    account = await engine.get(0, "account")
    await engine.set(0, "account", {**account, "generation": "replacement"})
    assert not (await preferences(engine, 0)).automatic_import
    await opt_in(engine, BackgroundImport(engine, Host([])))
    assert (await preferences(engine, 0)).automatic_import


async def test_new_play_for_same_title_never_resends_old_source_after_local_reset(harness):
    rows = [episode(1, 1, "2020-01-01T11:00:00Z")]
    stamp = "2026-01-01T00:00:00Z"

    def trakt(request):
        if request.url.path == "/sync/last_activities":
            return httpx.Response(200, json={"episodes": {"watched_at": stamp}})
        if request.url.path == "/sync/watched/shows":
            return httpx.Response(
                200, json=[{"show": {"ids": {"trakt": 800}}, "last_updated_at": stamp}]
            )
        if request.url.path in {"/sync/history/episodes", "/sync/history/shows/800"}:
            return httpx.Response(200, json=rows, headers={"X-Pagination-Page-Count": "1"})
        return None

    engine, _, clock, requests = await harness(trakt)
    host = Host([local_episode(1), local_episode(2)])
    worker = BackgroundImport(engine, host)
    await opt_in(engine, worker)
    await worker.process(0)
    assert host.units[1]["played"]
    # Simulate an undo and an absent host receipt. Only the plugin knows #1 is finished.
    host.records.clear()
    host.units[1].update(played=False, updated_at=iso_time(clock[0]))
    rows.append(episode(2, 2, "2020-01-02T11:00:00Z"))
    stamp = "2026-01-02T00:00:00Z"
    requests.clear()
    await BackgroundImport(engine, host).process(0)
    assert [r["episode"] for r in host.records.values()] == [2]
    assert not host.units[1]["played"]
    assert "/sync/history/episodes" not in [r.url.path for r in requests]
    assert "/sync/history/shows/800" in [r.url.path for r in requests]

    rows.append(episode(3, 1, "2020-01-03T11:00:00Z"))
    stamp = "2026-01-03T00:00:00Z"
    await worker.process(0)
    assert [r["episode"] for r in host.records.values()] == [2, 1]
    assert host.units[1]["played"]


async def test_catalog_mismatch_retries_after_title_becomes_matchable(harness):
    row = episode(1, 1, "2020-01-01T11:00:00Z")

    def trakt(request):
        if request.url.path == "/sync/last_activities":
            return httpx.Response(200, json={"episodes": {"watched_at": row["watched_at"]}})
        if request.url.path == "/sync/history/episodes":
            return httpx.Response(200, json=[row], headers={"X-Pagination-Page-Count": "1"})
        return None

    class ReidentifiedHost(Host):
        async def call(self, op, body):
            if op == "playback.sync.import" and self.units[1]["media"]["id"] == 70:
                return {"outcomes": [{"status": "mismatch"}]}
            return await super().call(op, body)

    engine, _, _, requests = await harness(trakt)
    host = ReidentifiedHost([local_episode(1)])
    worker = BackgroundImport(engine, host)
    await opt_in(engine, worker)
    await worker.process(0)
    assert not host.records
    host.units[1]["media"]["id"] = 71
    requests.clear()
    worker.inventory_changed()
    await worker.process(0)
    assert next(iter(host.records.values()))["media_item_id"] == 71
    assert not any(r.url.path.startswith("/sync/history") for r in requests)


async def test_watched_index_reads_all_pages_before_using_title_deltas(harness):
    def trakt(request):
        assert request.url.path == "/sync/watched/shows"
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=[{"show": {"ids": {"trakt": page}}, "last_updated_at": str(page)}],
            headers={"X-Pagination-Page-Count": "2"},
        )

    engine, _, _, _ = await harness(trakt)
    worker = BackgroundImport(engine, Host([]))
    await opt_in(engine, worker)
    account = await engine.get(0, "account")
    assert await worker.watched_snapshot(0, account["generation"], "episodes") == {
        "1": "1",
        "2": "2",
    }
