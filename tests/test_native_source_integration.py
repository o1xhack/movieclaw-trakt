"""Real host playback, persisted source event and catalog; only Trakt is mocked."""

import json
import logging
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from trakt_sync.client import TraktClient
from trakt_sync.engine import Engine
from trakt_sync.policy import iso_time
from trakt_sync.transfers import Transfers

from .conftest import Health, Store
from .test_host_contract import host as host
from .test_host_contract import seed
from .test_trakt_minutes import MinuteService
from .test_transfers import drive

pytestmark = pytest.mark.skipif(os.getenv("MOVIECLAW_HOST_QA") != "1", reason="isolated host QA")


@pytest.mark.parametrize("export_first", [False, True])
def test_real_native_source_receipt_survives_later_stop_and_export(host, monkeypatch, export_first):
    from movieclaw_api.services import durable_events
    from movieclaw_api.services.host_ops import HostOps
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import DomainEvent, EventConsumer
    from movieclaw_sdk.events import PLAYBACK_COMPLETED
    from sqlalchemy import select

    if "watch_source_ids" not in PLAYBACK_COMPLETED.payload.model_fields:
        pytest.skip("requires proposed native watch-source contract")
    app, c, login, root = host
    login(0)
    item_id = c.portal.call(seed, root / "source-media")
    clock = [datetime(2020, 1, 1, 12, 20)]
    for module in (
        "movieclaw_api.services.playback.watch",
        "movieclaw_playback.state",
        "movieclaw_api.services.playback_sync",
        "movieclaw_db.models",
    ):
        monkeypatch.setattr(module + ".utcnow", lambda: clock[0])

    async def scenario():
        async with get_database().session() as s:
            s.add(
                EventConsumer(
                    consumer_id="fixture-source",
                    event_name="playback.completed",
                    event_names=["playback.completed"],
                )
            )
            await s.commit()
        durable_events.reset_subscribed()
        service, native = MinuteService(), []
        lose_export = export_first

        def dispatch(request):
            nonlocal lose_export
            if request.url.path == "/scrobble/stop":
                native.append(json.loads(request.content))
                service.seed({"movie": {"ids": {"tmdb": 11}}}, iso_time(now()))
                return httpx.Response(201, json={"action": "scrobble"})
            response = service(request)
            if request.url.path == "/sync/history" and request.method == "POST" and lose_export:
                lose_export = False
                raise httpx.ReadError("fixture export response lost", request=request)
            return response

        def now():
            return clock[0].replace(tzinfo=timezone.utc).timestamp()

        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://fixture") as web,
            httpx.AsyncClient(transport=httpx.MockTransport(dispatch)) as remote,
        ):

            async def report(event, position=0):
                r = await web.post(
                    "/api/v1/playback/progress",
                    json={
                        "media_item_id": item_id,
                        "event": event,
                        "position_ms": position,
                        "device_id": "fixture-source",
                    },
                )
                assert r.status_code == 200, r.text

            await report("start")
            clock[0] = datetime(2020, 1, 1, 12, 34, 59)
            await report("progress", 570_000)
            async with get_database().session() as s:
                event = (
                    await s.execute(
                        select(DomainEvent).where(DomainEvent.name == "playback.completed")
                    )
                ).scalar_one()
                payload, event_id = event.payload, event.id
                occurred_at = event.occurred_at.isoformat() + "Z"
            source_id = payload["watch_source_ids"][0]
            assert source_id.startswith("playback:")
            store = Store()
            engine = Engine(
                store,
                TraktClient("fixture-client", "", remote),
                Health(),
                logging.getLogger("source"),
                clock=now,
            )
            await engine.set(
                0,
                "account",
                {
                    "access_token": "fixture",
                    "refresh_token": "fixture-r",
                    "expires_at": now() + 3600,
                    "generation": "source-binding",
                    "client_id": "fixture-client",
                },
                secret=True,
            )
            if not export_first:
                await engine.accept("completed", payload, event_id, occurred_at)
                clock[0] = datetime(2020, 1, 1, 12, 35, 1)
                await engine.drain(0)
            clock[0] = datetime(2020, 1, 1, 12, 45)
            await report("stop", 600_000)
            # Use the actual granted HostOps gateway, without the HTTP fixture login override.
            app.dependency_overrides.clear()
            ops = HostOps(app)
            ops.configure("fixture-source", grants=["playback.sync.catalog"])
            cleanup = []
            ctx = SimpleNamespace(
                entry_id="fixture-source",
                third_party=True,
                permissions=("playback.sync.catalog",),
                effect=lambda fn, **kw: cleanup.append(fn),
            )
            gateway = await ops.client(ctx)
            try:
                catalog = await gateway.call("playback.sync.catalog", {"member_id": 0})
                unit = catalog["items"][0]
                assert unit["watches"][0]["id"] == source_id
                assert unit["watches"][0]["watched_at"].startswith("2020-01-01T12:45:00")
                restored = Engine(store, engine.client, engine.health, engine.log, clock=now)
                transfers = Transfers(restored, gateway)
                task = await drive(transfers, await transfers.create(0, "export"), "ready")
                assert task["total"] == int(export_first)
                await transfers.confirm(0, task["id"], task["preview_token"])
                if export_first:
                    await transfers.step(task["id"], 0)
                    restored = Engine(store, engine.client, engine.health, engine.log, clock=now)
                    await restored.accept("completed", payload, event_id, occurred_at)
                    await restored.drain(0)
                    transfers = Transfers(restored, gateway)
                    clock[0] = datetime(2020, 1, 1, 12, 50)
                await drive(transfers, task, "complete")
                assert len(native) == int(not export_first)
                assert len(service.posts) == int(export_first)
                assert service.rows["movies"][0]["watched_at"] == (
                    "2020-01-01T12:45:00Z" if export_first else "2020-01-01T12:35:00Z"
                )
                assert not await restored.source_delivery(
                    2, "source-binding", {"movie": {"ids": {"tmdb": 11}}}, source_id
                )
                assert not await restored.source_delivery(
                    0, "another-binding", {"movie": {"ids": {"tmdb": 11}}}, source_id
                )
            finally:
                for fn in cleanup:
                    fn()
        durable_events.reset_subscribed()

    c.portal.call(scenario)
