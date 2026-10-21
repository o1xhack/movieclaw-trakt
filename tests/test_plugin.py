"""Exercise the published entrypoint with the real SDK kernel and HTTP routes."""

import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import Health, Store
from fastapi import FastAPI
from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_HEALTH, PLUGIN_ROUTES
from movieclaw_kernel import DURABLE_EVENTS, DeliveryInfo, Origin
from movieclaw_kernel.observe import current_delivery
from movieclaw_kernel.testing import KernelHarness
from movieclaw_sdk.events import PLAYBACK_COMPLETED, PLAYBACK_PROGRESS, Playback

from trakt_sync.application import DEFAULT_CLIENT_ID
from trakt_sync.engine import Engine
from trakt_sync.plugin import apply
from trakt_sync.policy import iso_time


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
    engines, requests, subscribers = [], [], {}
    store, app = Store(), FastAPI()

    class CaptureEngine(Engine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            engines.append(self)

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
        return httpx.Response(201, json={"action": "scrobble"})

    def subscribe(event, *, entry_id, listener_id, handler):
        key = event.name, entry_id, listener_id
        subscribers[key] = handler
        return lambda: subscribers.pop(key)

    def mount(ctx, router):
        app.include_router(router, prefix="/api/v1/plugins/trakt-sync")

    monkeypatch.setattr(entry, "Engine", CaptureEngine)
    monkeypatch.setattr(entry.net, "http_transport", lambda _: httpx.MockTransport(dispatch))
    async with KernelHarness() as h:
        h.provide(PLUGIN_DATA, SimpleNamespace(scoped=lambda _: store))
        h.provide(PLUGIN_HEALTH, SimpleNamespace(reporter=lambda _: Health()))
        h.provide(PLUGIN_ROUTES, SimpleNamespace(mount=mount))
        h.provide(DURABLE_EVENTS, SimpleNamespace(subscribe=subscribe))
        fiber = await h.mount(apply, config=config)
        assert fiber.state.value == "active"
        assert len(subscribers) == 3
        assert len(h.events.owned_by("trakt-sync")) == 1
        assert h.events.owned_by("trakt-sync")[0].event is PLAYBACK_PROGRESS
        prefix = "/api/v1/plugins/trakt-sync"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://fixture"
        ) as web:
            initial = (await web.get(prefix + "/status")).json()
            assert initial["configured"] and not initial["connected"]
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
            assert (await store.get("account", scope="member:0"))["client_id"] == expected_id
            assert "fixture-a" not in json.dumps(status)
            assert [r.url.path for r in requests].count("/scrobble/stop") == 1
            assert (await web.get(prefix + "/status?member_id=-1")).status_code == 422
        await h.unmount(fiber)
        assert not subscribers
        assert not h.events.owned_by("trakt-sync")
