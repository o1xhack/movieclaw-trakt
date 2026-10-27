"""Exercise the published entrypoint with the real SDK kernel and HTTP routes."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from movieclaw_api.api.deps import require_login
from movieclaw_api.exceptions import AppException
from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_HEALTH, PLUGIN_ROUTES
from movieclaw_api.services.auth import Principal
from movieclaw_api.services.plugin_routes import PluginRoutes, host_router
from movieclaw_kernel import DURABLE_EVENTS, DeliveryInfo, Origin
from movieclaw_kernel.observe import current_delivery
from movieclaw_kernel.testing import KernelHarness
from movieclaw_sdk.events import PLAYBACK_COMPLETED, PLAYBACK_PROGRESS, Playback
from movieclaw_sdk.ops import HOST_OPS

from trakt_sync.application import DEFAULT_CLIENT_ID
from trakt_sync.engine import Engine
from trakt_sync.plugin import apply
from trakt_sync.policy import iso_time

from .conftest import Health, Store


@pytest.mark.parametrize(
    ("config", "expected_id", "expected_secret"),
    [
        ({}, DEFAULT_CLIENT_ID, None),
        ({"client_id": ""}, DEFAULT_CLIENT_ID, None),
        ({"client_id": " \t", "client_secret": "stale-secret"}, DEFAULT_CLIENT_ID, None),
        ({"client_id": "fixture-client"}, "fixture-client", None),
        (
            {"client_id": " fixture-client ", "client_secret": "fixture-secret"},
            "fixture-client",
            "fixture-secret",
        ),
    ],
)
async def test_sdk_mount_authorize_deliver_duplicate_and_unmount(
    monkeypatch, event, config, expected_id, expected_secret
):
    import importlib

    entry = importlib.import_module("trakt_sync.plugin")
    engines, requests, subscribers, actions = [], [], {}, []
    store, app = Store(), FastAPI()

    class CaptureEngine(Engine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            engines.append(self)

    class CaptureActions(entry.Actions):
        def __init__(self, engine):
            super().__init__(engine)
            actions.append(self)

    def dispatch(request):
        requests.append(request)
        assert request.headers["trakt-api-key"] == expected_id
        if request.url.path == "/oauth/device/code":
            assert json.loads(request.content) == {"client_id": expected_id}
            return httpx.Response(
                200,
                json={
                    "verification_url": "https://trakt.tv/activate",
                    "device_code": "fixture-code",
                    "user_code": "TEST1234",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        if request.url.path == "/oauth/device/token":
            body = json.loads(request.content)
            assert body["client_id"] == expected_id
            assert body.get("client_secret") == expected_secret
            return httpx.Response(
                200,
                json={
                    "access_token": "fixture-a",
                    "refresh_token": "fixture-r",
                    "expires_in": 604800,
                },
            )
        if request.url.path == "/users/settings":
            return httpx.Response(200, json={"user": {"username": "fixture-user"}})
        if request.method == "GET" and request.url.path.startswith("/sync/history/"):
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"action": "scrobble"})

    def subscribe(event, *, entry_id, listener_id, handler):
        key = event.name, entry_id, listener_id
        subscribers[key] = handler
        return lambda: subscribers.pop(key)

    viewer = {"member_id": 0, "is_admin": True}

    async def principal():
        return Principal(
            kind="admin" if viewer["is_admin"] else "member",
            name="fixture",
            member_id=viewer["member_id"] if not viewer["is_admin"] else None,
            is_admin=viewer["is_admin"],
        )

    app.dependency_overrides[require_login] = principal

    @app.exception_handler(AppException)
    async def host_exception(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"message": str(exc)})

    routes = PluginRoutes(app, lambda _: store)

    monkeypatch.setattr(entry, "Engine", CaptureEngine)
    monkeypatch.setattr(entry, "Actions", CaptureActions)
    monkeypatch.setattr(entry.net, "http_transport", lambda _: httpx.MockTransport(dispatch))
    async with KernelHarness() as h:
        h.provide(PLUGIN_DATA, SimpleNamespace(scoped=lambda _: store))
        h.provide(PLUGIN_HEALTH, SimpleNamespace(reporter=lambda _: Health()))
        h.provide(PLUGIN_ROUTES, routes)

        async def ops_client(ctx):
            return SimpleNamespace(call=AsyncMock(return_value=[]))

        h.provide(HOST_OPS, SimpleNamespace(client=ops_client))
        h.provide(DURABLE_EVENTS, SimpleNamespace(subscribe=subscribe))
        fiber = await h.mount(apply, config=config)
        assert fiber.state.value == "active"
        app.include_router(host_router, prefix="/api/v1")
        assert len(subscribers) == 6
        assert len(h.events.owned_by("trakt-sync")) == 1
        assert h.events.owned_by("trakt-sync")[0].event is PLAYBACK_PROGRESS
        prefix = "/api/v1/plugins/trakt-sync"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://fixture"
        ) as web:
            initial = (await web.get(prefix + "/status")).json()
            assert initial["configured"] and not initial["connected"]
            assert (await web.get(prefix + "/accounts")).json()["accounts"][0]["member_id"] == 0
            assert (await web.get(prefix + "/activity")).json()["records"] == []
            assert (await web.post(prefix + "/auth/begin")).status_code == 200
            assert (await web.post(prefix + "/auth/poll")).json()["state"] == "connected"
            payload = Playback.model_validate(
                {**event, "media": {**event["media"], "title": "Fixture"}}
            )
            delivery = DeliveryInfo(
                "sdk-event", PLAYBACK_COMPLETED.name, iso_time(engines[0].clock()), Origin()
            )
            token = current_delivery.set(delivery)
            try:
                handler = subscribers[PLAYBACK_COMPLETED.name, "trakt-sync", "track-completed"]
                await handler(payload)
                await handler(payload)
            finally:
                current_delivery.reset(token)
            await engines[0].drain(0)
            status = (await web.get(prefix + "/status")).json()
            assert status["connected"] and status["pending"] == 0
            activity = (await web.get(prefix + "/activity")).json()
            assert activity["records"][0]["state"] == "sent"
            assert activity["records"][0]["title"] == "Fixture"
            assert "fixture-a" not in json.dumps(activity)
            assert (await web.post(prefix + "/retry/missing")).status_code == 409
            assert (await store.get("account", scope="member:0"))["client_id"] == expected_id
            assert "fixture-a" not in json.dumps(status)
            assert [r.url.path for r in requests].count("/scrobble/stop") == 1
            assert (await web.get(prefix + "/status?member_id=-1")).status_code == 422
            profile = {"manual_watched": True, "manual_unwatched": False, "favorites": True}
            assert (await web.put(prefix + "/profile", json=profile)).status_code == 200
            assert (await web.get(prefix + "/profile")).json()["preferences"] == profile
            assert (await web.get(prefix + "/profile?member_id=2")).json()["preferences"] != profile
            # 从真实 SDK 事件入口验证新增订阅，而非只调用 Actions 本身。
            from movieclaw_sdk.events import ItemFavorite, PlaybackMarked

            for name, kind, payload in (
                (
                    "playback.marked_played",
                    "marked_played",
                    PlaybackMarked(
                        member_id=0, media=event["media"] | {"title": "Fixture"}, units=()
                    ),
                ),
                (
                    "library.item.favorited",
                    "favorited",
                    ItemFavorite(
                        member_id=0, media=event["media"] | {"title": "Fixture"}, level="movie"
                    ),
                ),
            ):
                token = current_delivery.set(
                    DeliveryInfo("sdk-" + kind, name, iso_time(engines[0].clock()), Origin())
                )
                try:
                    await subscribers[name, "trakt-sync", "sync-" + kind](payload)
                finally:
                    current_delivery.reset(token)
            for _ in range(6):
                await actions[0].drain_once()
            assert (
                len([r for r in requests if r.method == "POST" and r.url.path == "/sync/history"])
                == 1
            )
            assert len([r for r in requests if r.url.path == "/sync/watchlist"]) == 1
            assert (await web.get(prefix + "/history")).json()["tasks"] == []
            assert (
                await web.post(prefix + "/history", json={"direction": "invalid"})
            ).status_code == 409
            assert (await web.get(prefix + "/viewer")).json()["management"] == "admin_only"
            assert (
                await web.put(prefix + "/profile", json={**profile, "ratings": True})
            ).status_code == 422
            viewer.update(member_id=2, is_admin=False)
            for path in ("status", "profile", "activity", "accounts", "viewer"):
                assert (await web.get(prefix + f"/{path}?member_id=0")).status_code == 403
            assert (await web.put(prefix + "/profile?member_id=0", json=profile)).status_code == 403
            assert (await web.post(prefix + "/auth/begin?member_id=0")).status_code == 403
        await h.unmount(fiber)
        assert not subscribers
        assert not h.events.owned_by("trakt-sync")
