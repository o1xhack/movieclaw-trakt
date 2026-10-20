import asyncio
import json

import httpx
import pytest

from trakt_sync.engine import Engine
from trakt_sync.policy import identity, iso_time, progress


@pytest.mark.parametrize(
    "media",
    [
        {"kind": "movie", "title": "Guess me"},
        {"kind": "photo", "tmdb_id": 11},
        {"kind": "movie", "tmdb_id": -1},
        {"kind": "movie", "imdb_id": "javascript:bad"},
    ],
)
def test_never_guess_identity(media):
    assert identity({"media": media}) is None


def test_episode_identity_uses_show_and_season_episode():
    assert identity(
        {"media": {"kind": "tv", "tmdb_id": 9}, "unit": {"season": 0, "episode": 2}}
    ) == {"show": {"ids": {"tmdb": 9}}, "episode": {"season": 0, "number": 2}}
    assert identity({"media": {"kind": "tv", "tmdb_id": 9}, "unit": None}) is None


@pytest.mark.parametrize(
    "position,duration,expected",
    [(0, 0, None), (9, None, None), (-1, 100, None), (50, 100, 50), (200, 100, 100)],
)
def test_progress(position, duration, expected):
    assert progress({"position_ms": position, "duration_ms": duration}) == expected


async def test_start_stop_completed_duplicate_is_one_scrobble(harness, event):
    e, store, clock, requests = await harness()
    await e.accept("started", {**event, "position_ms": 0}, "start", iso_time(clock[0]))
    await e.drain(0)
    clock[0] += 100
    for kind, key in [("stopped", "stop"), ("completed", "done"), ("stopped", "stop")]:
        await e.accept(kind, event, key, iso_time(clock[0]))
    await e.drain(0)
    await e.drain(0)
    assert [r.url.path for r in requests] == ["/scrobble/start", "/scrobble/stop"]
    assert (await e.status(0))["pending"] == 0


async def test_pause_stop_below_threshold_does_not_create_history(harness, event):
    e, _, clock, requests = await harness()
    await e.accept("stopped", {**event, "position_ms": 50000}, "pause", iso_time(clock[0]))
    await e.drain(0)
    assert json.loads(requests[0].content)["progress"] == 50
    assert requests[0].url.path == "/scrobble/stop"


async def test_restart_keeps_queue_and_duplicate_ledger(harness, event):
    e, store, clock, requests = await harness()
    await e.accept("completed", event, "done", iso_time(clock[0]))
    restarted = Engine(store, e.client, e.health, e.log, clock=e.clock)
    await restarted.accept("completed", event, "done", iso_time(clock[0]))
    await restarted.drain(0)
    assert len(requests) == 1
    assert requests[0].url.path == "/scrobble/stop"


async def test_member_isolation_no_admin_fallback(harness, event):
    e, _, clock, requests = await harness()
    await e.accept("completed", {**event, "member_id": 1}, "other", iso_time(clock[0]))
    await e.drain(1)
    assert not requests
    await e.set(
        1,
        "account",
        {
            "access_token": "member-one",
            "refresh_token": "member-one-r",
            "expires_at": clock[0] + 6000,
            "generation": "one",
            "client_id": "fixture-client",
        },
        secret=True,
    )
    await e.accept("completed", {**event, "member_id": 1}, "other", iso_time(clock[0]))
    await e.drain(1)
    assert requests[0].headers["authorization"] == "Bearer member-one"
    assert (await e.status(0))["pending"] == 0


async def test_old_event_preserves_actual_watched_at(harness, event):
    e, _, clock, requests = await harness()
    when = iso_time(clock[0] - 86400)
    await e.accept("started", event, "old-start", when)
    await e.accept("completed", event, "old-done", when)
    await e.accept("stopped", event, "old-stop", when)
    await e.drain(0)
    assert [r.url.path for r in requests] == ["/sync/history/movies", "/sync/history"]
    assert json.loads(requests[-1].content)["movies"][0]["watched_at"] == when


async def test_rate_limit_obeys_retry_after(harness, event):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "120"})

    e, _, clock, requests = await harness(handler)
    await e.accept("completed", event, "done", iso_time(clock[0]))
    await e.drain(0)
    clock[0] += 119
    await e.drain(0)
    assert len(requests) == 1
    clock[0] += 1
    await e.drain(0)
    assert [r.url.path for r in requests] == [
        "/scrobble/stop",
        "/sync/history/movies",
        "/sync/history",
    ]
    assert not (await e.status(0))["pending"]


async def test_lost_stop_response_checks_history_before_retry(harness, event):
    def handler(request):
        if request.url.path == "/scrobble/stop":
            raise httpx.ReadError("response lost", request=request)
        if request.url.path.startswith("/sync/history/"):
            return httpx.Response(
                200, json=[{"watched_at": iso_time(clock[0] - 60), "movie": {"ids": {"tmdb": 11}}}]
            )

    e, _, clock, requests = await harness(handler)
    await e.accept("completed", event, "done", iso_time(clock[0]))
    await e.drain(0)
    clock[0] += 60
    await e.drain(0)
    assert [r.url.path for r in requests] == ["/scrobble/stop", "/sync/history/movies"]
    assert not (await e.status(0))["pending"]


async def test_trakt_duplicate_conflict_is_success(harness, event):
    e, _, clock, _ = await harness(lambda r: httpx.Response(409))
    await e.accept("completed", event, "done", iso_time(clock[0]))
    await e.drain(0)
    assert not (await e.status(0))["pending"]


async def test_bad_item_does_not_block_following_items(harness, event):
    e, _, clock, _ = await harness(lambda r: httpx.Response(404))
    await e.accept("completed", event, "done", iso_time(clock[0]))
    await e.drain(0)
    assert (await e.status(0))["failed"] == 1
    assert not (await e.status(0))["pending"]
    assert (await e.retry_failed(0))["retried"] == 1
    assert (await e.status(0))["pending"] == 1


async def test_refresh_rotation_is_single_use_and_saved_first(harness, event):
    refreshes = 0

    def handler(request):
        nonlocal refreshes
        if request.url.path == "/oauth/token":
            refreshes += 1
            assert json.loads(request.content)["refresh_token"] == "fixture-refresh"
            return httpx.Response(
                200,
                json={
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "created_at": clock[0],
                    "expires_in": 604800,
                },
            )
        assert store.data["member:0", "account"]["refresh_token"] == "rotated-refresh"
        assert request.headers["authorization"] == "Bearer rotated-access"

    e, store, clock, _ = await harness(handler)
    account = await e.get(0, "account")
    account["expires_at"] = clock[0]
    await e.set(0, "account", account, secret=True)
    await e.accept("completed", event, "done", iso_time(clock[0]))
    await asyncio.gather(e.drain(0), e.drain(0))
    assert refreshes == 1
    assert ("member:0", "account") in store.secrets


async def test_failed_rotation_write_never_reuses_old_refresh(harness, event):
    refreshes = 0

    def handler(request):
        nonlocal refreshes
        if request.url.path == "/oauth/token":
            refreshes += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "new-a",
                    "refresh_token": "new-r",
                    "created_at": clock[0],
                    "expires_in": 604800,
                },
            )

    e, store, clock, requests = await harness(handler)
    account = await e.get(0, "account")
    account["expires_at"] = clock[0]
    await e.set(0, "account", account, secret=True)
    await e.accept("completed", event, "done", iso_time(clock[0]))
    store.fail_account_write = True
    with pytest.raises(OSError):
        await e.drain(0)
    await e.drain(0)
    assert refreshes == 1
    assert (await e.get(0, "account"))["refresh_token"] == "new-r"
    assert requests[-1].headers["authorization"] == "Bearer new-a"


async def test_disconnect_clears_local_queue_and_stops_future_events(harness, event):
    e, store, clock, requests = await harness()
    await e.accept("completed", event, "done", iso_time(clock[0]))
    await e.disconnect(0)
    await e.accept("completed", event, "done-again", iso_time(clock[0]))
    await e.drain(0)
    assert not (await e.status(0))["connected"]
    assert [r.url.path for r in requests] == ["/oauth/revoke"]
    assert not await store.get("ledger", scope="member:0")


async def test_new_client_id_cannot_reuse_old_app_credentials(harness, event):
    e, _, clock, requests = await harness()
    e.client.client_id = "different-client"
    await e.accept("completed", event, "done", iso_time(clock[0]))
    await e.drain(0)
    assert not requests
    assert not (await e.status(0))["connected"]


async def test_late_write_reuses_rotated_token_across_history_check_and_write(harness, event):
    refreshes = 0

    def handler(request):
        nonlocal refreshes
        if request.url.path == "/oauth/token":
            refreshes += 1
            assert json.loads(request.content)["refresh_token"] == "fixture-refresh"
            return httpx.Response(
                200,
                json={
                    "access_token": "rotated-a",
                    "refresh_token": "rotated-r",
                    "created_at": clock[0],
                    "expires_in": 604800,
                },
            )
        assert request.headers["authorization"] == "Bearer rotated-a"

    e, store, clock, requests = await harness(handler)
    account = await e.get(0, "account")
    account["expires_at"] = clock[0]
    await e.set(0, "account", account, secret=True)
    await e.accept("completed", event, "old", iso_time(clock[0] - 1000))
    await e.drain(0)
    assert refreshes == 1
    assert [r.url.path for r in requests] == [
        "/oauth/token",
        "/sync/history/movies",
        "/sync/history",
    ]
    assert not (await e.status(0))["pending"]


async def test_process_exit_after_remote_success_never_blindly_resends(harness, event):
    def handler(request):
        if request.url.path == "/scrobble/stop":
            assert store.data["member:0", "ledger"]["queue"][0]["uncertain"]
            raise RuntimeError("fixture process exit after remote commit")
        if request.url.path == "/sync/history/movies":
            return httpx.Response(
                200, json=[{"movie": {"ids": {"tmdb": 11}}, "watched_at": iso_time(clock[0])}]
            )

    e, store, clock, requests = await harness(handler)
    await e.accept("completed", event, "done", iso_time(clock[0]))
    with pytest.raises(RuntimeError):
        await e.drain(0)
    restarted = Engine(store, e.client, e.health, e.log, clock=e.clock)
    await restarted.drain(0)
    assert [r.url.path for r in requests] == ["/scrobble/stop", "/sync/history/movies"]
    assert not (await restarted.status(0))["pending"]


async def test_late_unmatched_result_is_visible_failure(harness, event):
    def handler(request):
        if request.url.path == "/sync/history":
            return httpx.Response(201, json={"not_found": {"movies": [{"ids": {"tmdb": 11}}]}})

    e, _, clock, _ = await harness(handler)
    await e.accept("completed", event, "late", iso_time(clock[0]))
    clock[0] += 600
    await e.drain(0)
    assert (await e.status(0))["failed"] == 1
    assert (await e.status(0))["last_error"]


async def test_seek_near_end_start_is_never_converted_to_watched_history(harness, event):
    e, _, clock, requests = await harness()
    await e.accept("started", event, "seek", iso_time(clock[0]))
    clock[0] += 600
    await e.drain(0)
    assert requests == []
    assert not (await e.status(0))["pending"]


async def test_short_stop_422_is_expected_noop_not_matching_failure(harness, event):
    e, _, clock, requests = await harness(lambda r: httpx.Response(422))
    await e.accept("stopped", {**event, "position_ms": 0}, "short", iso_time(clock[0]))
    await e.drain(0)
    status = await e.status(0)
    assert requests[0].url.path == "/scrobble/stop"
    assert status["pending"] == status["failed"] == 0
    assert not status["last_error"]


async def test_ordinary_422_keeps_failure_visible(harness, event):
    e, _, clock, _ = await harness(lambda r: httpx.Response(422))
    await e.accept("stopped", event, "invalid", iso_time(clock[0]))
    await e.drain(0)
    assert (await e.status(0))["failed"] == 1


async def test_legacy_short_pause_queue_can_be_retried_without_false_alarm(harness, event):
    e, _, clock, _ = await harness(lambda r: httpx.Response(422))
    await e.accept("stopped", {**event, "position_ms": 0}, "legacy", iso_time(clock[0]))
    ledger = await e.ledger(0)
    ledger["queue"][0]["path"] = "/scrobble/pause"
    await e.save_ledger(0, ledger)
    await e.drain(0)
    assert (await e.status(0))["failed"] == 0
    assert (await e.status(0))["last_success"]
