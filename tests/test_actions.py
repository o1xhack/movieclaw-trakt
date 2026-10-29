import json

import httpx
import pytest

from trakt_sync.actions import Actions
from trakt_sync.policy import iso_time
from trakt_sync.preferences import Preferences


async def drive(t, member, task_id, target, limit=1000):
    for _ in range(limit):
        task = await t.get(member, task_id)
        if task["state"] == target:
            return task
        await t.step(task_id, member)
    raise AssertionError(await t.get(member, task_id))


async def test_item_400_is_terminal_and_does_not_block_later_actions(harness, event):
    rejected = [True]

    def handler(request):
        if request.method == "POST" and request.url.path == "/sync/history" and rejected[0]:
            return httpx.Response(400)

    e, store, clock, requests = await harness(handler)
    await e.set(0, "profile", Preferences(manual_watched=True).model_dump())
    a = Actions(e)
    await a.automatic("marked_played", event, "bad", iso_time(clock[0]))
    first = next(iter((await store.scopes("task", entity="transfer")).values()))
    await a.step(first["id"], 0)
    task = await a.get(0, first["id"])
    assert task["state"] == "running" and not task.get("error")
    await a.step(first["id"], 0)
    assert (await a.get(0, first["id"]))["state"] == "complete_with_errors"
    assert (await e.status(0))["connected"]
    rejected[0] = False
    await a.automatic("marked_played", event, "good", iso_time(clock[0]))
    await a.drain_once()
    assert sum(r.method == "POST" and r.url.path == "/sync/history" for r in requests) == 2


async def test_action_auth_rejection_blocks_new_events_without_losing_other_members(harness, event):
    e, store, clock, requests = await harness(
        lambda r: httpx.Response(403) if r.headers["Authorization"] != "Bearer member-two" else None
    )
    await e.set(0, "profile", Preferences(favorites=True).model_dump())
    a = Actions(e)
    await a.automatic("favorited", {**event, "level": "movie"}, "auth-bad", iso_time(clock[0]))
    task = next(iter((await store.scopes("task", entity="transfer")).values()))
    await a.step(task["id"], 0)
    assert (await e.status(0))["needs_reconnect"]
    for n in range(70):
        await a.automatic("favorited", {**event, "level": "movie"}, f"auth-{n}", iso_time(clock[0]))
    assert len(await store.scopes("task", entity="transfer")) == 1
    assert len(requests) == 1
    await e.set(2, "profile", Preferences(favorites=True).model_dump())
    await e.set(
        2,
        "account",
        {
            **await e.get(0, "account"),
            "blocked": False,
            "access_token": "member-two",
            "generation": "two",
        },
        secret=True,
    )
    await a.automatic(
        "favorited", {**event, "member_id": 2, "level": "movie"}, "two", iso_time(clock[0])
    )
    await a.drain_once()
    assert len(requests) == 2 and requests[-1].headers["Authorization"] == "Bearer member-two"
    assert (await e.status(2))["connected"]


async def test_manual_opt_in_per_member_duplicates_unwatch_and_favorite(harness, event):
    e, _, clock, requests = await harness()
    t = Actions(e)
    when = iso_time(clock[0])
    await t.automatic("marked_played", event, "off", when)
    assert not await e.store.scopes("task", entity="transfer")
    await e.set(
        0,
        "profile",
        Preferences(manual_watched=True, manual_unwatched=True, favorites=True).model_dump(),
    )
    for _ in range(2):
        await t.automatic("marked_played", event, "same-event", when)
    tasks = (await e.store.scopes("task", entity="transfer")).values()
    assert len(tasks) == 1
    task = next(iter(tasks))
    await drive(t, 0, task["id"], "complete")
    assert [r.url.path for r in requests].count("/sync/history") == 1
    await t.automatic("marked_unplayed", event, "unwatch", when)
    await t.automatic(
        "favorited",
        {**event, "media": {**event["media"], "kind": "tv"}, "level": "series"},
        "favorite",
        when,
    )
    for task in (await e.store.scopes("task", entity="transfer")).values():
        await drive(t, 0, task["id"], "complete")
    assert [r.url.path for r in requests].count("/sync/history/remove") == 1
    favorite = next(r for r in requests if r.url.path == "/sync/watchlist")
    assert json.loads(favorite.content) == {"shows": [{"ids": {"tmdb": 11}}]}
    await t.automatic("marked_played", {**event, "member_id": 2}, "member-off", when)
    assert len(await e.store.scopes("task", entity="transfer")) == 3


async def test_disabling_preference_before_delivery_cancels_queued_action(harness, event):
    e, _, clock, requests = await harness()
    await e.set(0, "profile", Preferences(manual_watched=True).model_dump())
    t = Actions(e)
    await t.automatic("marked_played", event, "disable", iso_time(clock[0]))
    task = next(iter((await e.store.scopes("task", entity="transfer")).values()))
    await e.set(0, "profile", Preferences().model_dump())
    await t.step(task["id"], 0)
    assert (await t.get(0, task["id"]))["state"] == "cancelled"
    assert not requests


async def test_retry_after_restart_uses_remote_history_and_removes_without_timestamp(
    harness, event
):
    written = []
    failed = [False]

    def handler(request):
        if request.method == "POST" and request.url.path == "/sync/history":
            written.append(json.loads(request.content)["movies"][0])
            if not failed[0]:
                failed[0] = True
                return httpx.Response(503)
        if request.url.path == "/sync/history/movies":
            return httpx.Response(
                200,
                json=[
                    {"movie": {"ids": {"tmdb": 11}}, "watched_at": row["watched_at"]}
                    for row in written
                ],
            )

    e, store, clock, requests = await harness(handler)
    await e.set(0, "profile", Preferences(manual_watched=True, manual_unwatched=True).model_dump())
    a = Actions(e)
    await a.automatic("marked_played", event, "watched", iso_time(clock[0]))
    task = next(iter((await store.scopes("task", entity="transfer")).values()))
    await a.step(task["id"], 0)
    pending = await a.get(0, task["id"])
    assert pending["state"] == "running" and pending["next_at"] > clock[0]
    clock[0] = pending["next_at"] + 1
    await drive(Actions(e), 0, task["id"], "complete")
    assert len(written) == 1
    await a.automatic("marked_unplayed", event, "unwatch", iso_time(clock[0]))
    task = list((await store.scopes("task", entity="transfer")).values())[-1]
    await drive(a, 0, task["id"], "complete")
    body = json.loads(next(r for r in requests if r.url.path == "/sync/history/remove").content)
    assert "watched_at" not in body["movies"][0]


async def test_member_account_isolation_disconnect_and_ordering(harness, event):
    e, store, clock, requests = await harness()
    for member in (0, 2):
        await e.set(
            member, "profile", Preferences(manual_watched=True, manual_unwatched=True).model_dump()
        )
    await e.set(
        2,
        "account",
        {**await e.get(0, "account"), "access_token": "member-two", "generation": "two"},
        secret=True,
    )
    a = Actions(e)
    await a.automatic("marked_played", {**event, "member_id": 2}, "two-watch", iso_time(clock[0]))
    task = next(iter((await store.scopes("task", entity="transfer")).values()))
    await drive(a, 2, task["id"], "complete")
    assert all(r.headers["Authorization"] == "Bearer member-two" for r in requests)
    with pytest.raises(ValueError):
        await a.control(0, task["id"], "cancel")
    requests.clear()
    await a.automatic("marked_played", event, "watch", iso_time(clock[0]))
    await a.automatic("marked_unplayed", event, "unwatch", iso_time(clock[0]))
    tasks = [
        t for t in (await store.scopes("task", entity="transfer")).values() if t["member_id"] == 0
    ]
    tasks[0]["state"] = "paused"
    await a.save(tasks[0])
    await a.drain_once()
    assert not requests  # 暂停的标记操作不能被后面的取消操作越过。
    await a.control(0, tasks[0]["id"], "cancel")
    await a.drain_once()
    assert [r.url.path for r in requests] == ["/sync/history/remove"]
    await e.disconnect(0)
    await a.drain_once()
    assert (await a.get(0, tasks[1]["id"]))["state"] == "cancelled"


async def test_season_chunks_failure_retry_and_watchlist_levels(harness, event):
    rejected = [True]

    def handler(request):
        if request.method == "POST" and request.url.path == "/sync/history" and rejected[0]:
            rejected[0] = False
            return httpx.Response(422)

    e, store, clock, requests = await harness(handler)
    await e.set(0, "profile", Preferences(manual_watched=True, favorites=True).model_dump())
    a = Actions(e)
    media = {**event["media"], "kind": "tv"}
    await a.automatic(
        "marked_played",
        {**event, "media": media, "units": [{"season": 1, "episode": i} for i in range(1, 42)]},
        "season",
        iso_time(clock[0]),
    )
    task = next(iter((await store.scopes("task", entity="transfer")).values()))
    assert task["total"] == 41 and task["plan_pages"] == 2
    task = await drive(a, 0, task["id"], "complete_with_errors")
    assert task["done"] == 40 and task["failed"] == 1
    assert (await e.activity(0))["records"][0]["state"] == "failed"
    await a.control(0, task["id"], "resume")
    task = await drive(Actions(e), 0, task["id"], "complete")
    assert task["done"] == 41 and task["failed"] == 0
    for level, number in [("series", None), ("season", None), ("episode", 3)]:
        await a.automatic(
            "favorited",
            {**event, "media": media, "level": level, "season": 1, "episode": number},
            "favorite-" + level,
            iso_time(clock[0]),
        )
        task = list((await store.scopes("task", entity="transfer")).values())[-1]
        await drive(a, 0, task["id"], "complete")
    bodies = [
        json.loads(r.content)["shows"][0] for r in requests if r.url.path == "/sync/watchlist"
    ]
    assert "seasons" not in bodies[0]
    assert bodies[1]["seasons"] == [{"number": 1}]
    assert bodies[2]["seasons"] == [{"number": 1, "episodes": [{"number": 3}]}]


@pytest.mark.parametrize(
    ("option", "kind"),
    [
        ("manual_watched", "marked_played"),
        ("manual_unwatched", "marked_unplayed"),
        ("favorites", "favorited"),
    ],
)
async def test_each_switch_only_enables_its_own_event(harness, event, option, kind):
    e, store, clock, _ = await harness()
    await e.set(0, "profile", Preferences(**{option: True}).model_dump())
    a = Actions(e)
    for other in ("marked_played", "marked_unplayed", "favorited"):
        await a.automatic(
            other, {**event, "level": "movie"}, "independent-" + other, iso_time(clock[0])
        )
    tasks = list((await store.scopes("task", entity="transfer")).values())
    assert len(tasks) == 1 and tasks[0]["event"] == kind


async def test_rate_limit_keeps_task_and_respects_retry_after(harness, event):
    limited = [True]

    def handler(request):
        if request.url.path == "/sync/watchlist" and limited[0]:
            limited[0] = False
            return httpx.Response(429, headers={"Retry-After": "120"})

    e, store, clock, requests = await harness(handler)
    await e.set(0, "profile", Preferences(favorites=True).model_dump())
    a = Actions(e)
    await a.automatic("favorited", {**event, "level": "movie"}, "limited", iso_time(clock[0]))
    task = next(iter((await store.scopes("task", entity="transfer")).values()))
    await a.step(task["id"], 0)
    task = await a.get(0, task["id"])
    assert task["next_at"] >= clock[0] + 120 and task["state"] == "running"
    assert (await e.activity(0))["records"][0]["state"] == "retrying"
    await a.step(task["id"], 0)
    assert len(requests) == 1
    clock[0] = task["next_at"] + 1
    await drive(Actions(e), 0, task["id"], "complete")
    assert len(requests) == 2


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("episode", [False, True])
async def test_unwatch_supersedes_older_playback_and_late_redelivery(
    harness, event, failed, episode
):
    reject = [failed]

    def handler(r):
        if r.method == "POST" and r.url.path == "/sync/history" and reject[0]:
            return httpx.Response(422)

    e, store, clock, requests = await harness(handler)
    if episode:
        event = {
            **event,
            "media": {**event["media"], "kind": "tv"},
            "unit": {"season": 1, "episode": 2},
        }
    await e.set(0, "profile", Preferences(manual_unwatched=True).model_dump())
    old = iso_time(clock[0] - 600)
    await e.accept("completed", event, "old-completion", old)
    if failed:
        await e.drain(0)
        assert len((await e.ledger(0))["failed"]) == 1
    requests.clear()
    a = Actions(e)
    await a.automatic(
        "marked_unplayed",
        {**event, "units": [event["unit"]] if episode else []},
        "unwatch",
        iso_time(clock[0]),
    )
    task = next(iter((await store.scopes("task", entity="transfer")).values()))
    await drive(a, 0, task["id"], "complete")
    reject[0] = False
    await e.retry_failed(0)
    await e.drain(0)
    assert [r.url.path for r in requests if r.method == "POST"] == ["/sync/history/remove"]
    # A new engine instance must read the persisted cutoff after a restart.
    e = type(e)(store, e.client, e.health, e.log, clock=e.clock)
    # Another durable consumer may deliver the older completion only after the unwatch.
    await e.accept("completed", event, "late-old-completion", old)
    await e.drain(0)
    assert [r.url.path for r in requests if r.method == "POST"] == ["/sync/history/remove"]
    clock[0] += 1
    await e.accept("completed", event, "new-watch", iso_time(clock[0] - 0.5))
    await e.drain(0)
    assert [r.url.path for r in requests if r.method == "POST"] == [
        "/sync/history/remove",
        "/scrobble/stop",
    ]


async def test_unwatch_barrier_is_unit_member_and_binding_scoped(harness, event):
    e, store, clock, requests = await harness()
    event = {
        **event,
        "media": {**event["media"], "kind": "tv"},
        "unit": {"season": 1, "episode": 2},
    }
    account = await e.get(0, "account")
    from trakt_sync.policy import identity

    await e.remember_unwatch(0, account["generation"], identity(event), 7, iso_time(clock[0]))
    other = {**event, "unit": {"season": 1, "episode": 3}}
    await e.accept("completed", other, "other-episode", iso_time(clock[0] - 600))
    await e.drain(0)
    assert json.loads(requests[-1].content)["shows"][0]["seasons"][0]["episodes"][0]["number"] == 3
    await e.set(
        2, "account", {**account, "generation": "member-two", "access_token": "two"}, secret=True
    )
    await e.accept("completed", {**event, "member_id": 2}, "other-member", iso_time(clock[0] - 600))
    await e.drain(2)
    assert requests[-1].headers["Authorization"] == "Bearer two"
    await e.set(0, "account", {**account, "generation": "new-binding"}, secret=True)
    await e.accept("completed", event, "new-binding", iso_time(clock[0] - 600))
    await e.drain(0)
    assert len([r for r in requests if r.method == "POST"]) == 3


async def test_retry_old_failed_manual_watch_cannot_undo_unwatch(harness, event):
    reject = [True]

    def handler(r):
        if r.method == "POST" and r.url.path == "/sync/history" and reject[0]:
            return httpx.Response(422)

    e, store, clock, requests = await harness(handler)
    await e.set(0, "profile", Preferences(manual_watched=True, manual_unwatched=True).model_dump())
    a = Actions(e)
    await a.automatic("marked_played", event, "old-mark", iso_time(clock[0]))
    old = next(iter((await store.scopes("task", entity="transfer")).values()))
    await drive(a, 0, old["id"], "complete_with_errors")
    clock[0] += 1
    await a.automatic("marked_unplayed", event, "unmark", iso_time(clock[0]))
    unmark = list((await store.scopes("task", entity="transfer")).values())[-1]
    await drive(a, 0, unmark["id"], "complete")
    reject[0] = False
    before = len(requests)
    await a.control(0, old["id"], "resume")
    result = await drive(a, 0, old["id"], "complete")
    assert result["skipped"] == 1 and len(requests) == before


async def test_newer_playback_waits_for_pending_unwatch(harness, event):
    e, store, clock, requests = await harness()
    await e.set(0, "profile", Preferences(manual_unwatched=True).model_dump())
    a = Actions(e)
    await a.automatic("marked_unplayed", event, "unmark", iso_time(clock[0]))
    task = next(iter((await store.scopes("task", entity="transfer")).values()))
    clock[0] += 1
    await e.accept("completed", event, "new-watch", iso_time(clock[0]))
    await e.drain(0)
    assert not requests
    await drive(a, 0, task["id"], "complete")
    clock[0] += 3
    await e.drain(0)
    assert [r.url.path for r in requests if r.method == "POST"] == [
        "/sync/history/remove",
        "/scrobble/stop",
    ]


@pytest.mark.parametrize("manual", [False, True])
async def test_older_unwatch_cannot_remove_a_newer_delivered_watch(harness, event, manual):
    e, store, clock, requests = await harness()
    await e.set(0, "profile", Preferences(manual_watched=True, manual_unwatched=True).model_dump())
    a = Actions(e)
    older = iso_time(clock[0] - 10)
    if manual:
        await a.automatic("marked_played", event, "new-mark", iso_time(clock[0]))
        task = next(iter((await store.scopes("task", entity="transfer")).values()))
        await drive(a, 0, task["id"], "complete")
    else:
        await e.accept("completed", event, "new-watch", iso_time(clock[0]))
        await e.drain(0)
    before = len(requests)
    await a.automatic("marked_unplayed", event, "late-unmark", older)
    task = list((await store.scopes("task", entity="transfer")).values())[-1]
    result = await drive(a, 0, task["id"], "complete")
    assert result["skipped"] == 1 and len(requests) == before
