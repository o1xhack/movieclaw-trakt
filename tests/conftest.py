import copy
import logging

import httpx
import pytest

from trakt_sync.client import TraktClient
from trakt_sync.engine import Engine


class Store:
    def __init__(self):
        self.data = {}
        self.secrets = set()
        self.fail_account_write = False

    async def get(self, key, *, scope="global", default=None):
        return copy.deepcopy(self.data.get((scope, key), default))

    async def set(self, key, value, *, scope="global", secret=False):
        if key == "account" and self.fail_account_write:
            self.fail_account_write = False
            raise OSError("fixture storage interruption")
        self.data[scope, key] = copy.deepcopy(value)
        if secret:
            self.secrets.add((scope, key))

    async def delete(self, key, *, scope="global"):
        return self.data.pop((scope, key), None) is not None

    async def scopes(self, key, *, entity=None):
        return {
            scope: copy.deepcopy(value)
            for (scope, name), value in self.data.items()
            if name == key and (entity is None or scope.startswith(entity + ":"))
        }


class Health:
    async def degraded(self, key, message, **kwargs):
        pass

    async def ok(self, key):
        pass


@pytest.fixture
def event():
    return {
        "member_id": 0,
        "media": {"id": 7, "kind": "movie", "tmdb_id": 11},
        "unit": None,
        "position_ms": 90000,
        "duration_ms": 100000,
        "client": {"device_id": "fixture-device"},
    }


@pytest.fixture
async def harness():
    clients = []

    async def make(handler=None):
        clock = [1800000000.0]
        requests = []

        async def dispatch(request):
            requests.append(request)
            if handler:
                result = handler(request)
                if hasattr(result, "__await__"):
                    result = await result
                if result is not None:
                    return result
            if request.url.path.startswith("/sync/history/"):
                return httpx.Response(200, json=[])
            return httpx.Response(201, json={"action": "scrobble"})

        http = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
        clients.append(http)
        store = Store()
        client = TraktClient("fixture-client", "fixture-secret", http)
        engine = Engine(store, client, Health(), logging.getLogger("test"), clock=lambda: clock[0])
        await engine.set(
            0,
            "account",
            {
                "access_token": "fixture-access",
                "refresh_token": "fixture-refresh",
                "expires_at": clock[0] + 6000,
                "generation": "fixture-generation",
                "client_id": "fixture-client",
            },
            secret=True,
        )
        return engine, store, clock, requests

    yield make
    for client in clients:
        await client.aclose()
