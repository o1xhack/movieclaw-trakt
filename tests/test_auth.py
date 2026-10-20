import json

import httpx
import pytest

from trakt_sync.client import TraktError


async def test_device_authorization_polls_at_server_interval_and_hides_secrets(harness):
    def handler(request):
        if request.url.path == "/oauth/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "private-device",
                    "user_code": "ABCD1234",
                    "verification_url": "https://trakt.tv/activate",
                    "interval": 5,
                    "expires_in": 600,
                },
            )
        if request.url.path == "/oauth/device/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "created_at": clock[0],
                    "expires_in": 604800,
                },
            )
        if request.url.path == "/users/settings":
            return httpx.Response(200, json={"user": {"username": "fixture-user"}})

    e, store, clock, requests = await harness(handler)
    await store.delete("account", scope="member:0")
    visible = await e.begin(0)
    assert "device_code" not in visible
    assert ("member:0", "pending") in store.secrets
    assert (await e.poll(0))["state"] == "connected"
    view = json.dumps(await e.status(0))
    assert "fixture-user" in view
    for secret in ["new-access", "new-refresh", "private-device", "fixture-secret"]:
        assert secret not in view
    assert not await e.get(0, "pending")


async def test_pending_poll_and_expiry(harness):
    def handler(request):
        if request.url.path == "/oauth/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "device",
                    "user_code": "ABCD",
                    "verification_url": "https://trakt.tv/activate",
                    "interval": 5,
                    "expires_in": 20,
                },
            )
        return httpx.Response(400)

    e, store, clock, requests = await harness(handler)
    await store.delete("account", scope="member:0")
    await e.begin(0)
    assert (await e.poll(0))["state"] == "pending"
    assert (await e.poll(0))["state"] == "pending"
    assert len(requests) == 2
    clock[0] += 21
    assert (await e.poll(0))["state"] == "expired"
    assert len(requests) == 2


async def test_refuse_rebind_without_explicit_disconnect(harness):
    e, _, _, requests = await harness()
    with pytest.raises(ValueError):
        await e.begin(0)
    assert not requests


@pytest.mark.parametrize(
    "url", ["http://trakt.tv/activate", "https://evil.invalid/", "javascript:alert(1)"]
)
async def test_no_untrusted_oauth_links(harness, url):
    e, store, _, _ = await harness(
        lambda r: httpx.Response(
            200, json={"verification_url": url, "user_code": "ABCD", "device_code": "device"}
        )
    )
    await store.delete("account", scope="member:0")
    with pytest.raises(TraktError):
        await e.begin(0)


async def test_official_auth_subdomain_returned_by_live_device_endpoint(harness):
    e, store, _, _ = await harness(
        lambda r: httpx.Response(
            200,
            json={
                "device_code": "fixture-device",
                "user_code": "TEST1234",
                "verification_url": "https://auth.trakt.tv/activate",
                "interval": 5,
                "expires_in": 600,
            },
        )
    )
    await store.delete("account", scope="member:0")
    result = await e.begin(0)
    assert result["verification_url"] == "https://auth.trakt.tv/activate"
    assert "device_code" not in result
