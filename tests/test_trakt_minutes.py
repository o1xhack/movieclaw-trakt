"""Isolated Trakt #694 contract: store/read watched_at at UTC minute precision."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from trakt_sync.actions import Actions
from trakt_sync.engine import Engine
from trakt_sync.policy import history_body, identity, timestamp
from trakt_sync.preferences import Preferences
from trakt_sync.transfers import Transfers

from .test_transfers import Ops, drive, unit


def service_time(value):
    # Independent fake-server contract, not the plugin's time helper.
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(timezone.utc)
        .replace(second=0, microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class MinuteService:
    def __init__(self, *, lose_response=False):
        self.rows = {"movies": [], "episodes": []}
        self.posts = []
        self.lose_response = lose_response

    def seed(self, target, when):
        kind = "movies" if "movie" in target else "episodes"
        row = (
            {"movie": target["movie"]}
            if kind == "movies"
            else {
                "show": target["show"],
                "episode": {
                    "season": target["episode"]["season"],
                    "number": target["episode"]["number"],
                },
            }
        )
        row["watched_at"] = service_time(when)
        if not any({k: v for k, v in r.items() if k != "id"} == row for r in self.rows[kind]):
            row["id"] = sum(map(len, self.rows.values())) + 1
            self.rows[kind].append(row)
            return 1
        return 0

    def __call__(self, request):
        if request.url.path.startswith("/sync/history/") and request.method == "GET":
            kind = request.url.path.rsplit("/", 1)[-1]
            start = timestamp(request.url.params.get("start_at", "1900-01-01T00:00:00Z"))
            end = timestamp(request.url.params.get("end_at", "2099-01-01T00:00:00Z"))
            found = [r for r in self.rows[kind] if start <= timestamp(r["watched_at"]) <= end]
            return httpx.Response(200, json=found, headers={"X-Pagination-Page-Count": "1"})
        if request.url.path == "/sync/history" and request.method == "POST":
            body = json.loads(request.content)
            self.posts.append(body)
            if "movies" in body:
                value = body["movies"][0]
                kind = "movies"
                added = self.seed({"movie": {"ids": value["ids"]}}, value["watched_at"])
            else:
                show = body["shows"][0]
                season = show["seasons"][0]
                value = season["episodes"][0]
                kind = "episodes"
                added = self.seed(
                    {
                        "show": {"ids": show["ids"]},
                        "episode": {"season": season["number"], "number": value["number"]},
                    },
                    value["watched_at"],
                )
            if self.lose_response:
                self.lose_response = False
                return httpx.Response(503)
            return httpx.Response(201, json={"added": {kind: added}})


def local(kind="movie", when="2020-01-01T12:34:45.678Z"):
    row = unit(11, kind, season=1 if kind == "tv" else 0, episode=2 if kind == "tv" else 0)
    row["watches"][0]["watched_at"] = when
    return row


def target(row):
    return identity({"media": row["media"], "unit": row})


@pytest.mark.parametrize("kind", ["movie", "tv"])
async def test_export_matches_minute_normalized_history_with_seconds_in_source(harness, kind):
    service = MinuteService()
    row = local(kind)
    service.seed(target(row), row["watches"][0]["watched_at"])
    e, _, _, requests = await harness(service)
    t = Transfers(e, Ops([row]))
    task = await drive(t, await t.create(0, "export"), "ready")
    await t.confirm(0, task["id"], task["preview_token"])
    await drive(t, task, "complete")
    assert not service.posts
    query = requests[0].url.params
    assert timestamp(query["start_at"]) <= timestamp("2020-01-01T12:34:00Z")
    assert timestamp(query["end_at"]) >= timestamp("2020-01-01T12:34:00Z")


@pytest.mark.parametrize("kind", ["movie", "tv"])
@pytest.mark.parametrize("neighbor", ["2020-01-01T12:33:00Z", "2020-01-01T12:35:00Z"])
async def test_engine_does_not_confuse_adjacent_minutes(harness, kind, neighbor):
    service = MinuteService()
    row = local(kind, "2020-01-01T12:34:00Z")
    service.seed(target(row), neighbor)
    e, _, _, _ = await harness(service)
    assert not await e.history_exists(
        0,
        await e.get(0, "account"),
        {"item": target(row), "occurred_at": row["watches"][0]["watched_at"]},
    )


@pytest.mark.parametrize("kind", ["movie", "tv"])
async def test_export_lost_response_reconciles_minute_after_restart(harness, kind):
    service = MinuteService(lose_response=True)
    row = local(kind)
    e, _, clock, _ = await harness(service)
    ops = Ops([row])
    t = Transfers(e, ops)
    task = await drive(t, await t.create(0, "export"), "ready")
    await t.confirm(0, task["id"], task["preview_token"])
    await t.step(task["id"], 0)
    retained = await t.get(0, task["id"])
    clock[0] = retained["next_at"] + 1
    await drive(Transfers(e, ops), task, "complete")
    assert len(service.posts) == 1
    assert sum(map(len, service.rows.values())) == 1


@pytest.mark.parametrize("kind", ["movie", "tv"])
def test_history_payload_is_utc_minute_without_mutating_source(kind):
    row = local(kind, "2020-01-01T04:34:59.999-08:00")
    before = json.dumps(row)
    body = history_body(target(row), row["watches"][0]["watched_at"])
    value = body["movies"][0] if kind == "movie" else body["shows"][0]["seasons"][0]["episodes"][0]
    assert value["watched_at"] == "2020-01-01T12:34:00Z"
    assert json.dumps(row) == before


@pytest.mark.parametrize("kind", ["movie", "tv"])
async def test_same_minute_watches_are_disclosed_not_counted_as_lossless_export(harness, kind):
    row = local(kind)
    row["watches"] = [
        {"id": f"watch:{i}", "watched_at": when, "basis": "playback"}
        for i, when in enumerate(
            ["2020-01-01T12:34:10.100Z", "2020-01-01T12:34:55.900Z", "2020-01-01T12:35:01Z"]
        )
    ]
    service = MinuteService()
    e, _, _, _ = await harness(service)
    t = Transfers(e, Ops([row]))
    task = await drive(t, await t.create(0, "export"), "ready")
    assert task["minute_collisions"] == 1
    await t.confirm(0, task["id"], task["preview_token"])
    done = await drive(t, task, "complete_with_errors")
    assert done["done"] == 2 and done["skipped"] == 1
    assert len(service.posts) == 2 and sum(map(len, service.rows.values())) == 2
    details = await t.details(0, task["id"], records=True)
    assert "分钟" in next(r for r in details["items"] if r["state"] == "skipped")["reason"]
    assert len(row["watches"]) == 3  # Local source facts stay intact.
    again = await drive(t, await t.create(0, "export"), "ready")
    assert again["total"] == 0


async def test_manual_same_minute_actions_share_one_remote_watch(harness, event):
    service = MinuteService()
    e, store, _, _ = await harness(service)
    await e.set(0, "profile", Preferences(manual_watched=True).model_dump())
    a = Actions(e)
    for i, when in enumerate(["2020-01-01T12:34:10.100Z", "2020-01-01T12:34:55.900Z"]):
        await a.automatic("marked_played", event, f"mark-{i}", when)
        tasks = list((await store.scopes("task", entity="transfer")).values())
        for _ in range(3):
            await a.step(tasks[-1]["id"], 0)
    assert len(service.posts) == 1 and len(service.rows["movies"]) == 1


async def test_failed_first_source_then_same_minute_success_stays_one_watch_on_retry(harness):
    row = local()
    row["watches"].append(
        {**row["watches"][0], "id": "second", "watched_at": "2020-01-01T12:34:55Z"}
    )
    service = MinuteService()
    rejected = [True]

    def dispatch(request):
        if request.method == "POST" and request.url.path == "/sync/history" and rejected[0]:
            rejected[0] = False
            return httpx.Response(400)
        return service(request)

    e, _, _, _ = await harness(dispatch)
    t = Transfers(e, Ops([row]))
    task = await drive(t, await t.create(0, "export"), "ready")
    await t.confirm(0, task["id"], task["preview_token"])
    first = await drive(t, task, "complete_with_errors")
    assert first["failed"] == 1 and first["done"] == 1
    await t.control(0, task["id"], "resume")
    done = await drive(t, task, "complete_with_errors")
    assert done["done"] == 1 and done["skipped"] == 1 and not done["failed"]
    assert len(service.posts) == 1


async def test_incremental_new_same_minute_watch_discloses_previously_exported_owner(harness):
    row, service = local(), MinuteService()
    e, _, _, _ = await harness(service)
    t = Transfers(e, Ops([row]))
    first = await drive(t, await t.create(0, "export"), "ready")
    await t.confirm(0, first["id"], first["preview_token"])
    await drive(t, first, "complete")
    row["watches"].insert(
        0, {**row["watches"][0], "id": "later-source", "watched_at": "2020-01-01T12:34:59Z"}
    )
    again = await drive(t, await t.create(0, "export"), "ready")
    assert again["total"] == 1 and again["minute_collisions"] == 1
    await t.confirm(0, again["id"], again["preview_token"])
    done = await drive(t, again, "complete_with_errors")
    assert done["done"] == 0 and done["skipped"] == 1 and len(service.posts) == 1


async def test_same_minute_collision_spanning_persisted_plan_pages(harness):
    row = local()
    row["watches"] = [{**row["watches"][0], "id": f"source:{i}"} for i in range(31)]
    service = MinuteService()
    e, _, _, _ = await harness(service)
    t = Transfers(e, Ops([row]))
    task = await drive(t, await t.create(0, "export"), "ready")
    assert task["plan_pages"] == 2 and task["minute_collisions"] == 30
    await t.confirm(0, task["id"], task["preview_token"])
    done = await drive(t, task, "complete_with_errors")
    assert done["done"] == 1 and done["skipped"] == 30 and len(service.posts) == 1


async def test_collapsed_receipt_restores_skipped_result_after_storage_interruption(harness):
    row, service = local(), MinuteService()
    row["watches"].append({**row["watches"][0], "id": "second"})
    e, store, clock, _ = await harness(service)
    t = Transfers(e, Ops([row]))
    task = await drive(t, await t.create(0, "export"), "ready")
    await t.confirm(0, task["id"], task["preview_token"])
    original_set = store.set
    failed = [False]

    async def interrupt(key, value, **kw):
        if key == "plan_0" and any(r["state"] == "skipped" for r in value) and not failed[0]:
            failed[0] = True
            raise OSError("lost plan write after merged receipt")
        return await original_set(key, value, **kw)

    store.set = interrupt
    await t.step(task["id"], 0)
    retained = await t.get(0, task["id"])
    clock[0] = retained["next_at"] + 1
    done = await drive(Transfers(e, t.ops), task, "complete_with_errors")
    assert done["done"] == 1 and done["skipped"] == 1 and len(service.posts) == 1


@pytest.mark.parametrize("kind", ["movie", "tv"])
async def test_export_does_not_match_next_minute_at_fractional_boundary(harness, kind):
    row, service = local(kind, "2020-01-01T12:34:59.999999Z"), MinuteService()
    service.seed(target(row), "2020-01-01T12:35:00Z")
    e, _, _, _ = await harness(service)
    t = Transfers(e, Ops([row]))
    task = await drive(t, await t.create(0, "export"), "ready")
    await t.confirm(0, task["id"], task["preview_token"])
    done = await drive(t, task, "complete")
    assert done["done"] == 1 and len(service.posts) == 1
    assert len(service.rows["movies" if kind == "movie" else "episodes"]) == 2


@pytest.mark.parametrize("kind", ["movie", "tv"])
@pytest.mark.parametrize("timing", ["dispatch", "arrival", "process_exit", "refresh", "refresh401"])
async def test_uncertain_native_scrobble_matches_dispatch_window_after_restart(
    harness, kind, timing
):
    service = MinuteService()
    row = local(kind, "2020-01-01T12:34:59Z")
    event = {
        "member_id": 0,
        "media": row["media"],
        "unit": row,
        "position_ms": 100,
        "duration_ms": 100,
        "client": {"device_id": "fixture"},
    }

    def handler(request):
        if request.url.path == "/oauth/token":
            clock[0] += 61
            return httpx.Response(
                200, json={"access_token": "new", "refresh_token": "new-r", "expires_in": 3600}
            )
        if request.url.path == "/scrobble/stop":
            # The server assigns watched_at; the request carries no original timestamp.
            assert "watched_at" not in json.loads(request.content)
            if (
                timing == "refresh401"
                and request.headers["Authorization"] == "Bearer fixture-access"
            ):
                clock[0] += 61
                return httpx.Response(401)
            if timing in {"arrival", "process_exit"}:
                clock[0] += 2
            service.seed(target(row), datetime.fromtimestamp(clock[0], timezone.utc).isoformat())
            if timing == "process_exit":
                raise RuntimeError("fixture process exit after native scrobble commit")
            raise httpx.ReadError("committed stop response lost", request=request)
        return service(request)

    e, store, clock, _ = await harness(handler)
    clock[0] = timestamp("2020-01-01T12:35:01Z" if timing == "dispatch" else "2020-01-01T12:34:59Z")
    if timing == "refresh":
        account = await e.get(0, "account")
        account["expires_at"] = clock[0]
        await e.set(0, "account", account, secret=True)
    await e.accept("completed", event, "native", row["watches"][0]["watched_at"])
    if timing == "process_exit":
        with pytest.raises(RuntimeError):
            await e.drain(0)
    else:
        await e.drain(0)
    clock[0] += 120
    restarted = Engine(store, e.client, e.health, e.log, clock=e.clock)
    await restarted.drain(0)
    assert not (await restarted.status(0))["pending"]
    assert not service.posts and sum(map(len, service.rows.values())) == 1


async def test_explicit_history_retry_clears_native_dispatch_window(harness, event):
    service = MinuteService(lose_response=True)

    def handler(request):
        if request.url.path == "/scrobble/stop":
            raise httpx.ReadError("uncommitted native request", request=request)
        return service(request)

    e, store, clock, _ = await harness(handler)
    clock[0] = timestamp("2020-01-01T12:35:01Z")
    await e.accept("completed", event, "native", "2020-01-01T12:34:59Z")
    await e.drain(0)
    for _ in range(2):
        clock[0] += 120
        e = Engine(store, e.client, e.health, e.log, clock=e.clock)
        await e.drain(0)
    assert not (await e.status(0))["pending"]
    assert len(service.posts) == 1
    assert service.rows["movies"][0]["watched_at"] == "2020-01-01T12:34:00Z"


async def test_observed_native_error_does_not_match_a_later_distinct_watch(harness, event):
    service = MinuteService()

    def handler(request):
        if request.url.path == "/scrobble/stop":
            raise httpx.ReadError("uncommitted native request", request=request)
        return service(request)

    e, _, clock, _ = await harness(handler)
    clock[0] = timestamp("2020-01-01T12:35:01Z")
    await e.accept("completed", event, "native", "2020-01-01T12:34:59Z")
    await e.drain(0)
    service.seed({"movie": {"ids": {"tmdb": 11}}}, "2020-01-01T12:36:00Z")
    clock[0] += 120
    await e.drain(0)
    assert len(service.posts) == 1  # A later, unrelated watch cannot cover the failed request.


@pytest.mark.parametrize("status", [400, 429])
async def test_definite_native_rejection_does_not_reuse_dispatch_window(harness, event, status):
    service = MinuteService()

    def handler(request):
        if request.url.path == "/scrobble/stop":
            return httpx.Response(status)
        return service(request)

    e, _, clock, _ = await harness(handler)
    clock[0] = timestamp("2020-01-01T12:35:01Z")
    service.seed({"movie": {"ids": {"tmdb": 11}}}, "2020-01-01T12:35:00Z")
    await e.accept("completed", event, "native", "2020-01-01T12:34:59Z")
    await e.drain(0)
    clock[0] += 1200
    await e.drain(0)
    assert len(service.posts) == 1
    assert len(service.rows["movies"]) == 2


@pytest.mark.parametrize("refresh_failure", ["network", "server", "process_exit"])
async def test_rejected_stop_window_is_cleared_before_uncertain_token_refresh(
    harness, event, refresh_failure
):
    service = MinuteService()
    observed = []

    def handler(request):
        if request.url.path == "/scrobble/stop":
            return httpx.Response(401)
        if request.url.path == "/oauth/token":
            observed.append(store.data["member:0", "ledger"]["queue"][0].get("scrobble_window"))
            clock[0] += 61
            if refresh_failure == "process_exit":
                raise RuntimeError("fixture exit during token refresh after stop rejection")
            if refresh_failure == "network":
                raise httpx.ReadError("token response lost", request=request)
            return httpx.Response(503)
        return service(request)

    e, store, clock, _ = await harness(handler)
    clock[0] = timestamp("2020-01-01T12:35:01Z")
    service.seed({"movie": {"ids": {"tmdb": 11}}}, "2020-01-01T12:35:00Z")
    await e.accept("completed", event, "native", "2020-01-01T12:34:59Z")
    if refresh_failure == "process_exit":
        with pytest.raises(RuntimeError):
            await e.drain(0)
    else:
        await e.drain(0)
    clock[0] += 120
    restarted = Engine(store, e.client, e.health, e.log, clock=e.clock)
    await restarted.drain(0)
    assert len(service.posts) == 1 and len(service.rows["movies"]) == 2
    assert observed == [None]  # Rejected attempt removed durably before /oauth/token.
    assert not (await restarted.status(0))["pending"]
