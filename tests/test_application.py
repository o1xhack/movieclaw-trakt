import logging

import httpx
from conftest import Health, Store

from trakt_sync.application import DEFAULT_CLIENT_ID, application_credentials
from trakt_sync.client import TraktClient
from trakt_sync.engine import Engine


async def test_existing_dedicated_app_binding_survives_switch_to_empty_configuration():
    store = Store()
    await store.set(
        "account",
        {"access_token": "fixture-token", "client_id": DEFAULT_CLIENT_ID},
        scope="member:0",
        secret=True,
    )
    client_id, secret = application_credentials("", "unused-old-secret")
    async with httpx.AsyncClient() as http:
        engine = Engine(
            store, TraktClient(client_id, secret, http), Health(), logging.getLogger("test")
        )
        status = await engine.status(0)
        assert status["configured"] and status["connected"]
        assert not (await engine.status(1))["connected"]
        assert not secret
        assert (await store.get("account", scope="member:0"))["access_token"] == "fixture-token"
