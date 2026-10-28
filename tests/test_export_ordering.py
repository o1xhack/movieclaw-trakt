"""Successful historical exports participate in precise watch/unwatch ordering."""

import pytest

from trakt_sync.actions import Actions
from trakt_sync.engine import Engine
from trakt_sync.policy import timestamp
from trakt_sync.preferences import Preferences
from trakt_sync.transfers import Transfers

from .test_actions import drive as drive_actions
from .test_trakt_minutes import MinuteService, local, target
from .test_transfers import Ops, drive


@pytest.mark.parametrize("kind", ["movie", "tv"])
@pytest.mark.parametrize(
    "recovery", ["normal", "existing", "lost", "fingerprint", "alias", "barrier_failure"]
)
async def test_export_protects_newer_watch_from_late_unwatch(harness, monkeypatch, kind, recovery):
    when = "2020-01-01T12:35:45.500000Z"
    row, service = local(kind, when), MinuteService(lose_response=recovery == "lost")
    if recovery == "existing":
        service.seed(target(row), when)
    engine, store, clock, requests = await harness(service)
    clock[0] = timestamp("2020-01-01T12:50:00Z")
    await engine.set(0, "profile", Preferences(manual_unwatched=True).model_dump())
    if recovery == "barrier_failure":
        original_set = store.set
        interrupted = False

        async def fail_barrier_once(key, value, **kwargs):
            nonlocal interrupted
            if key in engine.unwatch_keys(target(row), row["media"]["id"], prefix="watch"):
                if not interrupted:
                    interrupted = True
                    raise OSError("fixture exported-watch marker commit interrupted")
            await original_set(key, value, **kwargs)

        monkeypatch.setattr(store, "set", fail_barrier_once)
    transfers = Transfers(engine, Ops([row]))
    task = await drive(transfers, await transfers.create(0, "export"), "ready")
    await transfers.confirm(0, task["id"], task["preview_token"])
    await transfers.step(task["id"], 0)
    clock[0] += 120
    engine = Engine(store, engine.client, engine.health, engine.log, clock=engine.clock)
    transfers = Transfers(engine, Ops([row]))
    await drive(transfers, task, "complete")
    if recovery in {"fingerprint", "alias"}:
        # Simulate durable receipts created before the ordering marker existed.
        for key in engine.unwatch_keys(target(row), row["media"]["id"], prefix="watch"):
            await engine.store.delete(key, scope=engine.scope(0))
        if recovery == "fingerprint":
            for scope, key in list(store.data):
                if scope == engine.scope(0) and key.startswith("watch_source_"):
                    await store.delete(key, scope=scope)
        else:
            watch = row["watches"][0]
            watch["source_ids"] = [watch["id"], "playback:new-anchor"]
            watch.update(id="playback:new-anchor", watched_at="2020-01-01T12:45:00Z")
        again = await drive(transfers, await transfers.create(0, "export"), "ready")
        assert again["total"] == 0
        await transfers.confirm(0, again["id"], again["preview_token"])
        await drive(transfers, again, "complete")
    event = {"member_id": 0, "media": row["media"], "units": [row]}
    actions = Actions(engine)
    await actions.automatic(
        "marked_unplayed", event, "older-unwatch", "2020-01-01T12:35:45.499999Z"
    )
    older = list((await store.scopes("task", entity="transfer")).values())[-1]
    outcome = await drive_actions(actions, 0, older["id"], "complete")
    assert outcome["skipped"] == 1
    assert not [r for r in requests if r.url.path == "/sync/history/remove"]
    # A genuinely newer unwatch must still work; use source precision, not the Trakt minute
    # or a catalog timestamp that changed after an alias migration.
    await actions.automatic(
        "marked_unplayed", event, "newer-unwatch", "2020-01-01T12:35:45.500001Z"
    )
    newer = list((await store.scopes("task", entity="transfer")).values())[-1]
    assert (await drive_actions(actions, 0, newer["id"], "complete"))["done"] == 1
    assert len([r for r in requests if r.url.path == "/sync/history/remove"]) == 1
    assert len(service.posts) == int(recovery != "existing")


@pytest.mark.parametrize("kind", ["movie", "tv"])
async def test_native_reconciled_export_marks_its_actual_source_time(harness, kind):
    row = local(kind, "2020-01-01T12:45:00Z")
    service = MinuteService(lose_response=True)
    engine, store, clock, requests = await harness(service)
    clock[0] = timestamp("2020-01-01T12:50:00Z")
    await engine.set(0, "profile", Preferences(manual_unwatched=True).model_dump())
    transfers = Transfers(engine, Ops([row]))
    task = await drive(transfers, await transfers.create(0, "export"), "ready")
    await transfers.confirm(0, task["id"], task["preview_token"])
    await transfers.step(task["id"], 0)  # remote commit, response lost
    engine = Engine(store, engine.client, engine.health, engine.log, clock=engine.clock)
    await engine.accept(
        "completed",
        {
            "member_id": 0,
            "media": row["media"],
            "unit": row,
            "position_ms": 100,
            "duration_ms": 100,
            "watch_source_ids": [row["watches"][0]["id"]],
        },
        "earlier-native-completion",
        "2020-01-01T12:35:00Z",
    )
    await engine.drain(0)
    # The late completion recovers the export at 12:45, before foreground resume.
    actions = Actions(engine)
    await actions.automatic(
        "marked_unplayed",
        {"member_id": 0, "media": row["media"], "units": [row]},
        "unwatch-between-completion-and-export",
        "2020-01-01T12:40:00Z",
    )
    unwatch = list((await store.scopes("task", entity="transfer")).values())[-1]
    assert (await drive_actions(actions, 0, unwatch["id"], "complete"))["skipped"] == 1
    assert len(service.posts) == 1
    assert not [r for r in requests if r.url.path in {"/scrobble/stop", "/sync/history/remove"}]
