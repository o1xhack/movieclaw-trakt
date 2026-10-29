"""Real host SQLite + HostOps HTTP gateway + plugin routes. Only Trakt is mocked."""

import asyncio
import importlib
import json
import os
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from .conftest import Health, Store

pytestmark = pytest.mark.skipif(
    os.getenv("MOVIECLAW_HOST_QA") != "1", reason="isolated host integration"
)


def test_matched_host_plugin_foreground_import_export_and_permissions(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from movieclaw_api.core.config import get_settings
    from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_HEALTH, PLUGIN_ROUTES
    from movieclaw_api.services.auth import issue_member_session_token, reset_auth_state
    from movieclaw_api.services.durable_events import DurableEvents
    from movieclaw_api.services.host_ops import HostOps
    from movieclaw_api.services.plugin_routes import PluginRoutes
    from movieclaw_api.settings.store import reset_setting_store
    from movieclaw_db.crypto import reset_secret_box
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
        EventConsumer,
        FileSource,
        FileState,
        LibraryFile,
        MediaItem,
        Member,
        PlaybackLog,
        PlaybackState,
        WatchRecord,
    )
    from movieclaw_db.repositories.library_repo import LibraryRepository
    from movieclaw_kernel import DURABLE_EVENTS
    from movieclaw_kernel.testing import KernelHarness
    from movieclaw_sdk.ops import HOST_OPS
    from sqlalchemy import select

    from trakt_sync.engine import Engine

    for key, value in {
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'pair.db'}",
        "SECRET_KEY_FILE": str(tmp_path / "key"),
        "MOVIECLAW_DATA_DIR": str(tmp_path),
        "SITE_CONFIGS_DIR": str(tmp_path / "sites"),
        "LOG_DIR": str(tmp_path / "logs"),
        "SCHEDULER_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    from movieclaw_api.app import create_app

    app = create_app()
    entry = importlib.import_module("trakt_sync.plugin")
    store, engines, requests = Store(), [], []
    episode_rows = []

    class QuietEngine(Engine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            engines.append(self)

    def dispatch(r):
        requests.append(r)
        if r.url.path == "/users/settings":
            return httpx.Response(
                200, json={"user": {"username": "fixture-two", "ids": {"uuid": "fixture-two-uuid"}}}
            )
        if r.url.path == "/sync/last_activities":
            return httpx.Response(200, json={"movies": {"watched_at": "2020-01-04T00:00:00Z"}})
        if r.method == "GET" and r.url.path == "/sync/history/movies":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": i,
                        "watched_at": f"2020-01-0{i}T11:00:00Z",
                        "movie": {"ids": {"tmdb": 11}},
                    }
                    for i in (1, 2, 3)
                ],
                headers={"X-Pagination-Page-Count": "1"},
            )
        if r.method == "GET" and r.url.path == "/sync/history/episodes":
            return httpx.Response(200, json=episode_rows, headers={"X-Pagination-Page-Count": "1"})
        return httpx.Response(201, json={"added": {"movies": 1}})

    monkeypatch.setattr(entry, "Engine", QuietEngine)
    monkeypatch.setattr(entry.net, "http_transport", lambda _: httpx.MockTransport(dispatch))

    async def scenario():
        async with get_database().session() as s:
            library = await LibraryRepository(s).create(
                name="fixture", kind="movie", root_paths=[str(tmp_path)]
            )
            media = MediaItem(
                kind="movie", title="Three watches", original_title="Fixture", tmdb_id=11
            )
            s.add(media)
            s.add(Member(id=2, username="two", password_hash="unused"))
            await s.flush()
            s.add(
                LibraryFile(
                    library_id=library.id,
                    media_item_id=media.id,
                    file_path=str(tmp_path / "m.mkv"),
                    size_bytes=1,
                    source=FileSource.SCANNED,
                    state=FileState.IN_PLACE,
                    duration_seconds=600,
                )
            )
            s.add(
                PlaybackState(
                    member_id=2,
                    media_item_id=media.id,
                    position_ms=120000,
                    last_played_at=datetime(2020, 1, 2, 12),
                    is_favorite=True,
                    audio_track="1",
                    subtitle_track="off",
                    play_count=5,
                )
            )
            await s.commit()
        ops = HostOps(app)
        ops.configure("trakt-sync", grants=list(entry.apply.permissions))
        # The host has its own live kernel; the harness leak scan is process-wide.
        async with KernelHarness(strict=False) as h:
            h.provide(PLUGIN_DATA, SimpleNamespace(scoped=lambda _: store))
            h.provide(PLUGIN_HEALTH, SimpleNamespace(reporter=lambda _: Health()))
            h.provide(PLUGIN_ROUTES, PluginRoutes(app, lambda _: store))
            h.provide(HOST_OPS, ops)

            events = DurableEvents(get_database())
            h.provide(DURABLE_EVENTS, events)
            fiber = await h.mount(entry.apply)
            assert fiber.state.value == "active"
            for _ in range(100):
                async with get_database().session() as s:
                    consumers = list((await s.execute(select(EventConsumer))).scalars())
                if len([v for v in consumers if v.consumer_id.startswith("trakt-sync:")]) == 6:
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("durable consumers did not register")
            e = engines[0]
            await e.set(
                2,
                "account",
                {
                    "access_token": "fixture",
                    "refresh_token": "fixture-r",
                    "expires_at": e.clock() + 3600,
                    "generation": "fixture-binding",
                    "client_id": e.client.client_id,
                    "username": "fixture-two",
                },
                secret=True,
            )
            # Real admin cookie and real plugin tokens exercise both authorization boundaries.
            from movieclaw_api.services.auth import create_admin, issue_session_token

            await create_admin("fixture-admin", "a-fixture-password-with-length")
            session, _ = await issue_session_token("fixture-admin")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://fixture"
            ) as web:
                web.cookies.set("movieclaw_session", session)
                prefix = "/api/v1/plugins/trakt-sync/history"

                async def call(path="", method="GET", body=None):
                    url = httpx.URL(prefix + path).copy_add_param("member_id", "2")
                    r = await web.request(method, url, json=body)
                    assert r.status_code == 200, r.text
                    return r.json().get("data", r.json())

                assert (await call())["tasks"] == [] and not requests
                task = await call("", "POST", {"direction": "import"})
                assert not requests
                for _ in range(10):
                    task = await call(f"/{task['id']}/tick", "POST")
                    if task["state"] == "ready":
                        break
                assert task["state"] == "ready" and task["total"] == 3
                async with get_database().session() as s:
                    assert not list((await s.execute(select(WatchRecord))).scalars())
                await call(f"/{task['id']}/confirm", "POST", {"token": task["preview_token"]})
                async with get_database().session() as s:
                    assert not list((await s.execute(select(WatchRecord))).scalars())
                for _ in range(10):
                    task = await call(f"/{task['id']}/tick", "POST")
                    if task["state"] == "complete":
                        break
                assert task["done"] == 3 and task["state"] == "complete", task
                async with get_database().session() as s:
                    facts = list((await s.execute(select(WatchRecord))).scalars())
                    assert sorted(v.watched_at for v in facts) == [
                        datetime(2020, 1, i, 11) for i in (1, 2, 3)
                    ]
                    assert len(facts) == 3 and all(
                        v.member_id == 2 and v.source == "plugin:trakt-sync" for v in facts
                    )
                    assert not list((await s.execute(select(PlaybackLog))).scalars())
                    states = list((await s.execute(select(PlaybackState))).scalars())
                    assert (
                        len(states) == 1
                        and states[0].last_played_at.year == 2020
                        and states[0].played
                        and states[0].position_ms == 0
                        and states[0].is_favorite
                        and states[0].audio_track == "1"
                        and states[0].subtitle_track == "off"
                        and states[0].play_count == 5
                    )
                data = await call(f"/{task['id']}?records=true")
                assert len(data["items"]) == 3
                # New binding and a renamed display username must still hit the real
                # host's existing source IDs, even without receipts for this generation.
                account = await e.get(2, "account")
                assert account["user_uuid"] == "fixture-two-uuid"
                await e.set(
                    2,
                    "account",
                    {**account, "generation": "rebound-fixture", "username": "renamed-fixture"},
                    secret=True,
                )
                rebound = await call("", "POST", {"direction": "import"})
                for _ in range(10):
                    rebound = await call(f"/{rebound['id']}/tick", "POST")
                    if rebound["state"] == "ready":
                        break
                assert rebound["state"] == "ready" and rebound["total"] == 3
                await call(f"/{rebound['id']}/confirm", "POST", {"token": rebound["preview_token"]})
                for _ in range(10):
                    rebound = await call(f"/{rebound['id']}/tick", "POST")
                    if rebound["state"] == "complete":
                        break
                assert rebound["state"] == "complete" and rebound["done"] == 3
                async with get_database().session() as s:
                    assert len(list((await s.execute(select(WatchRecord))).scalars())) == 3
                # Matching imported rewatches can be exported, and existing Trakt facts are not re-posted.
                exported = await call("", "POST", {"direction": "export"})
                for _ in range(10):
                    exported = await call(f"/{exported['id']}/tick", "POST")
                    if exported["state"] == "ready":
                        break
                assert exported["total"] == 3
                await call(
                    f"/{exported['id']}/confirm", "POST", {"token": exported["preview_token"]}
                )
                for _ in range(10):
                    exported = await call(f"/{exported['id']}/tick", "POST")
                    if exported["state"] == "complete":
                        break
                assert exported["done"] == 3
                assert not [r for r in requests if r.method == "POST"]
                # Real identity correction + cleanup must retain every imported watch and source key.
                async with get_database().session() as s:
                    corrected = MediaItem(
                        kind="movie", title="Corrected", original_title="Fixture", tmdb_id=12
                    )
                    s.add(corrected)
                    await s.flush()
                    file = (
                        await s.execute(
                            select(LibraryFile).where(LibraryFile.media_item_id == media.id)
                        )
                    ).scalar_one()
                    file.review_suggestion = {"media_item_id": corrected.id, "title": "Corrected"}
                    s.add(file)
                    await s.commit()
                    file_id, corrected_id = file.id, corrected.id
                r = await web.post(
                    "/api/v1/libraries/identification/review-decisions",
                    json={"file_ids": [file_id], "decision": "accept_suggestion"},
                )
                assert r.status_code == 200, r.text
                from movieclaw_api.services.media_scrape import cleanup_orphan_items
                from sqlalchemy import delete

                async with get_database().session() as s:
                    await s.execute(
                        delete(PlaybackState).where(PlaybackState.media_item_id == media.id)
                    )
                    await s.commit()
                assert await cleanup_orphan_items([media.id]) == 1
                async with get_database().session() as s:
                    facts = list((await s.execute(select(WatchRecord))).scalars())
                    assert len(facts) == 3 and all(w.media_item_id == corrected_id for w in facts)

                # Corrected identity exports the same three dates under the corrected external ID.
                corrected_export = await call("", "POST", {"direction": "export"})
                for _ in range(10):
                    corrected_export = await call(f"/{corrected_export['id']}/tick", "POST")
                    if corrected_export["state"] == "ready":
                        break
                assert corrected_export["total"] == 3
                await call(
                    f"/{corrected_export['id']}/confirm",
                    "POST",
                    {"token": corrected_export["preview_token"]},
                )
                for _ in range(10):
                    corrected_export = await call(f"/{corrected_export['id']}/tick", "POST")
                    if corrected_export["state"] == "complete":
                        break
                assert corrected_export["done"] == 3
                posted = [
                    json.loads(r.content)["movies"][0] for r in requests if r.method == "POST"
                ]
                assert len(posted) == 3 and all(r["ids"]["tmdb"] == 12 for r in posted)
                assert {r["watched_at"][:10] for r in posted} == {
                    "2020-01-01",
                    "2020-01-02",
                    "2020-01-03",
                }
                assert not await store.scopes("task", entity="transfer")
                # Real member cookie -> marks API -> durable SQLite event -> plugin -> Trakt.
                prefs = {"manual_watched": True, "manual_unwatched": True, "favorites": True}
                response = await web.put(
                    "/api/v1/plugins/trakt-sync/profile?member_id=2", json=prefs
                )
                assert response.status_code == 200, response.text
                async with get_database().session() as s:
                    member = await s.get(Member, 2)
                    member_session, _ = await issue_member_session_token(member)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://fixture"
                ) as member_web:
                    member_web.cookies.set("movieclaw_session", member_session)
                    assert (
                        await member_web.get("/api/v1/plugins/trakt-sync/profile")
                    ).status_code == 403
                    for change, expected in [
                        ({"played": True}, "/sync/history"),
                        ({"played": False}, "/sync/history/remove"),
                        ({"favorite": True}, "/sync/watchlist"),
                    ]:
                        before = len(requests)
                        response = await member_web.post(
                            "/api/v1/playback/marks", json={"media_item_id": corrected_id, **change}
                        )
                        assert response.status_code == 200, response.text
                        for _ in range(250):
                            tasks = list((await store.scopes("task", entity="transfer")).values())
                            if (
                                tasks
                                and all(v["state"] == "complete" for v in tasks)
                                and any(
                                    r.method == "POST" and r.url.path == expected
                                    for r in requests[before:]
                                )
                            ):
                                break
                            await asyncio.sleep(0.02)
                        else:
                            raise AssertionError(tasks)
                        assert all(v["member_id"] == 2 for v in tasks)
                        assert all(r.headers["Authorization"] == "Bearer fixture" for r in requests)
                # Trakt episode rewatches arrive at minute precision, independently of season sentinels.
                async with get_database().session() as s:
                    show = MediaItem(
                        kind="tv", title="Series", original_title="Series", tmdb_id=200
                    )
                    s.add(show)
                    await s.flush()
                    s.add(
                        LibraryFile(
                            library_id=library.id,
                            media_item_id=show.id,
                            season_number=1,
                            episode_number=2,
                            file_path=str(tmp_path / "s1e2.mkv"),
                            size_bytes=1,
                            source=FileSource.SCANNED,
                            state=FileState.IN_PLACE,
                        )
                    )
                    await s.commit()
                    show_id = show.id
                for i, when in enumerate(["2021-02-03T04:05:00Z", "2021-02-04T05:06:00Z"]):
                    episode_rows.append(
                        {
                            "id": 100 + i,
                            "watched_at": when,
                            "show": {"ids": {"tmdb": 200}},
                            "episode": {"season": 1, "number": 2},
                        }
                    )
                imported = await call("", "POST", {"direction": "import"})
                for _ in range(10):
                    imported = await call(f"/{imported['id']}/tick", "POST")
                    if imported["state"] == "ready":
                        break
                assert imported["total"] == 2 and imported["episodes"] == 1
                await call(
                    f"/{imported['id']}/confirm", "POST", {"token": imported["preview_token"]}
                )
                for _ in range(10):
                    imported = await call(f"/{imported['id']}/tick", "POST")
                    if imported["state"] == "complete":
                        break
                assert imported["done"] == 2
                async with get_database().session() as s:
                    watches = list(
                        (
                            await s.execute(
                                select(WatchRecord).where(WatchRecord.media_item_id == show_id)
                            )
                        ).scalars()
                    )
                    assert sorted(w.watched_at for w in watches) == [
                        datetime(2021, 2, 3, 4, 5),
                        datetime(2021, 2, 4, 5, 6),
                    ]
                    assert all(
                        w.season_number == 1 and w.episode_number == 2 and w.member_id == 2
                        for w in watches
                    )
                    assert not list((await s.execute(select(PlaybackLog))).scalars())
                assert len(await store.scopes("task", entity="transfer")) == 3
                # Existing native history keeps its seconds/microseconds in the real host DB.
                precise = datetime(2021, 3, 4, 5, 6, 47, 123456)
                async with get_database().session() as s:
                    native = MediaItem(
                        kind="movie", title="Native precision", original_title="Native", tmdb_id=333
                    )
                    s.add(native)
                    await s.flush()
                    s.add(
                        LibraryFile(
                            library_id=library.id,
                            media_item_id=native.id,
                            file_path=str(tmp_path / "native.mkv"),
                            size_bytes=1,
                            source=FileSource.SCANNED,
                            state=FileState.IN_PLACE,
                            duration_seconds=600,
                        )
                    )
                    s.add(
                        PlaybackState(
                            member_id=2,
                            media_item_id=native.id,
                            played=True,
                            last_played_at=precise,
                            play_count=1,
                        )
                    )
                    s.add(
                        PlaybackLog(
                            member_id=2,
                            media_item_id=native.id,
                            started_at=datetime(2021, 3, 4, 4, 56),
                            ended_at=precise,
                            last_seen_at=precise,
                            end_position_ms=570000,
                            completed=True,
                        )
                    )
                    await s.commit()
                    native_id = native.id
                native_export = await call("", "POST", {"direction": "export"})
                for _ in range(10):
                    native_export = await call(f"/{native_export['id']}/tick", "POST")
                    if native_export["state"] == "ready":
                        break
                await call(
                    f"/{native_export['id']}/confirm",
                    "POST",
                    {"token": native_export["preview_token"]},
                )
                for _ in range(10):
                    native_export = await call(f"/{native_export['id']}/tick", "POST")
                    if native_export["state"] == "complete":
                        break
                assert native_export["state"] == "complete"
                native_posts = [
                    json.loads(r.content)["movies"][0]
                    for r in requests
                    if r.method == "POST"
                    and r.url.path == "/sync/history"
                    and json.loads(r.content).get("movies", [{}])[0].get("ids", {}).get("tmdb")
                    == 333
                ]
                assert (
                    len(native_posts) == 1
                    and native_posts[0]["watched_at"] == "2021-03-04T05:06:00Z"
                )
                async with get_database().session() as s:
                    native_log = (
                        await s.execute(
                            select(PlaybackLog).where(PlaybackLog.media_item_id == native_id)
                        )
                    ).scalar_one()
                    assert native_log.ended_at == precise
                    native_state = (
                        await s.execute(
                            select(PlaybackState).where(PlaybackState.media_item_id == native_id)
                        )
                    ).scalar_one()
                    assert native_state.last_played_at == precise
            await h.unmount(fiber)
            await events.close()

            assert not [
                t
                for t in asyncio.all_tasks()
                if t.get_name().startswith("plugin:trakt-sync:") and not t.done()
            ]
            assert not h.events.owned_by("trakt-sync")

    try:
        with TestClient(app) as c:
            c.portal.call(scenario)
    finally:
        reset_setting_store()
        reset_secret_box()
        reset_auth_state()
        get_settings.cache_clear()
