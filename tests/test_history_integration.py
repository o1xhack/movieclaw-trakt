"""Real host SQLite + HostOps HTTP gateway + plugin routes. Only Trakt is mocked."""

import importlib
import json
import os
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from conftest import Health, Store

pytestmark = pytest.mark.skipif(
    os.getenv("MOVIECLAW_HOST_QA") != "1", reason="isolated host integration"
)


def test_matched_host_plugin_foreground_import_export_and_permissions(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from movieclaw_api.core.config import get_settings
    from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_HEALTH, PLUGIN_ROUTES
    from movieclaw_api.services.auth import reset_auth_state
    from movieclaw_api.services.host_ops import HostOps
    from movieclaw_api.services.plugin_routes import PluginRoutes
    from movieclaw_api.settings.store import reset_setting_store
    from movieclaw_db.crypto import reset_secret_box
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import (
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
    real_events = []

    class QuietEngine(Engine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            engines.append(self)

    def dispatch(r):
        requests.append(r)
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
            return httpx.Response(200, json=[], headers={"X-Pagination-Page-Count": "1"})
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
        ops.configure("trakt-sync", grants=["playback.sync.catalog", "playback.sync.import"])
        # The host has its own live kernel; the harness leak scan is process-wide.
        async with KernelHarness(strict=False) as h:
            h.provide(PLUGIN_DATA, SimpleNamespace(scoped=lambda _: store))
            h.provide(PLUGIN_HEALTH, SimpleNamespace(reporter=lambda _: Health()))
            h.provide(PLUGIN_ROUTES, PluginRoutes(app, lambda _: store))
            h.provide(HOST_OPS, ops)

            def subscribe(event, **kwargs):
                real_events.append(event)
                return lambda: None

            h.provide(DURABLE_EVENTS, SimpleNamespace(subscribe=subscribe))
            fiber = await h.mount(entry.apply)
            assert fiber.state.value == "active"
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
            await h.unmount(fiber)
            import asyncio

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
