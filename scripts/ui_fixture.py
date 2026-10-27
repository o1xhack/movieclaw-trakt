"""Local UI fixture: actual plugin routes, in-memory host data, mocked Trakt only.

Run from the repository with PYTHONPATH=. and the pinned host environment.
No production server, user credential, or real Trakt request is used.
"""

import asyncio
import importlib
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_HEALTH, PLUGIN_ROUTES
from movieclaw_kernel import DURABLE_EVENTS
from movieclaw_kernel.testing import KernelHarness
from movieclaw_sdk.ops import HOST_OPS

from tests.conftest import Health, Store
from tests.test_transfers import Ops, unit


async def main():
    app, store, remote_writes = FastAPI(), Store(), []
    entry = importlib.import_module("trakt_sync.plugin")
    inventory = [unit(watches=3)] + [unit(2, "tv", 1, e) for e in range(1, 13)]
    ops = Ops(inventory)

    async def dispatch(r):
        await asyncio.sleep(0.1)
        if r.method == "POST":
            remote_writes.append({"path": r.url.path, "body": json.loads(r.content)})
            return httpx.Response(201, json={"added": {"movies": 1, "episodes": 1}})
        if r.url.path == "/sync/last_activities":
            return httpx.Response(200, json={"movies": {"watched_at": "2020-01-04T00:00:00Z"}})
        return httpx.Response(200, json=[], headers={"X-Pagination-Page-Count": "1"})

    entry.net.http_transport = lambda _: httpx.MockTransport(dispatch)

    class Routes:
        def mount(self, ctx, router, **kwargs):
            app.include_router(router, prefix="/api/v1/plugins/trakt-sync")

    class Host:
        async def client(self, ctx):
            return ops

    async with KernelHarness(strict=False) as h:
        h.provide(PLUGIN_DATA, SimpleNamespace(scoped=lambda _: store))
        h.provide(PLUGIN_HEALTH, SimpleNamespace(reporter=lambda _: Health()))
        h.provide(PLUGIN_ROUTES, Routes())
        h.provide(HOST_OPS, Host())
        h.provide(DURABLE_EVENTS, SimpleNamespace(subscribe=lambda *a, **kw: lambda: None))
        fiber = await h.mount(entry.apply)
        assert fiber.state.value == "active"
        for member in (0, 2):
            await store.set(
                "account",
                {
                    "access_token": f"fixture-{member}",
                    "refresh_token": "fixture",
                    "client_id": entry.application_credentials("", "")[0],
                    "expires_at": 9999999999,
                    "generation": f"fixture-{member}",
                    "username": f"fixture-{member}",
                },
                scope=f"member:{member}",
                secret=True,
            )
            await store.set("linked", True, scope=f"member:{member}")

        @app.get("/__fixture/state")
        async def state():
            return {"remote_writes": len(remote_writes), "host_writes": len(ops.writes)}

        app.mount(
            "/apps/trakt-sync", StaticFiles(directory=Path(__file__).parents[1] / "web", html=True)
        )
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        print(f"http://127.0.0.1:{sock.getsockname()[1]}/apps/trakt-sync/", flush=True)
        try:
            await uvicorn.Server(uvicorn.Config(app, log_level="warning")).serve(sockets=[sock])
        finally:
            await h.unmount(fiber)


if __name__ == "__main__":
    asyncio.run(main())
