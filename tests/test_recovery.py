import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from movieclaw_sdk.ops import OpsError

from trakt_sync.engine import Engine
from trakt_sync.policy import iso_time
from trakt_sync.recovery import LocalStateError, MetadataResolver


@pytest.fixture
def episode(event):
    return {
        **event,
        "file_id": 3019,
        "media": {"id": 267, "kind": "tv", "tmdb_id": 9},
        "unit": {"season": 0, "episode": 1},
    }


def metadata_ops(*, state="in_place", missing=False, files=True, deleted=False):
    async def call(operation, args):
        if operation == "library.list":
            return [{"id": 1}, {"id": 2}]
        if args["library_id"] == 1 or deleted:
            raise OpsError(404, "NOT_FOUND", "fixture")
        return {
            "kind": "tv",
            "tmdb_id": 99,
            "files": [
                {
                    "id": 3019,
                    "season_number": 1,
                    "episode_number": 44,
                    "state": state,
                    "missing": missing,
                    "file_path": "/private/do-not-store.mkv",
                }
            ]
            if files
            else [],
        }

    return SimpleNamespace(call=AsyncMock(side_effect=call))


async def fail_episode(harness, episode):
    engine, store, clock, requests = await harness(lambda r: httpx.Response(404))
    await engine.accept("completed", episode, "failed", iso_time(clock[0]))
    await engine.drain(0)
    engine.client.http._transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    return engine, store, clock, requests


async def test_file_identity_refreshes_ids_and_units_without_retaining_paths():
    ops = metadata_ops()
    source = {"media_id": 267, "file_id": 3019}
    result = await MetadataResolver(ops)(source, {})
    assert result == {"show": {"ids": {"tmdb": 99}}, "episode": {"season": 1, "number": 44}}
    assert source == {"media_id": 267, "file_id": 3019, "library_id": 2}
    assert ops.call.await_count == 3
    assert "file_path" not in json.dumps(result)


@pytest.mark.parametrize(
    "options,deleted",
    [
        ({"files": False}, True),
        ({"deleted": True}, True),
        ({"missing": True}, False),
        ({"state": "pending_delete"}, False),
    ],
)
async def test_deleted_and_temporarily_missing_are_distinct(options, deleted):
    with pytest.raises(LocalStateError) as error:
        await MetadataResolver(metadata_ops(**options))({"media_id": 267, "file_id": 3019}, {})
    assert error.value.deleted is deleted


async def test_no_file_id_never_guesses_season_zero_is_season_one():
    with pytest.raises(LocalStateError, match="原季集已不存在"):
        await MetadataResolver(metadata_ops())(
            {"media_id": 267}, {"episode": {"season": 0, "number": 44}}
        )


async def test_automatic_repair_retains_time_and_checks_history_before_write(harness, episode):
    engine, store, clock, requests = await fail_episode(harness, episode)
    engine.resolver = MetadataResolver(metadata_ops())
    retried = []

    def dispatch(request):
        retried.append(request)
        return (
            httpx.Response(200, json=[])
            if request.method == "GET"
            else httpx.Response(201, json={})
        )

    engine.client.http._transport = httpx.MockTransport(dispatch)
    original = (await engine.ledger(0))["failed"][0]["occurred_at"]
    clock[0] += 600
    await engine.drain(0)
    assert [r.url.path for r in retried] == ["/sync/history/episodes", "/sync/history"]
    body = json.loads(retried[-1].content)
    assert body["shows"][0] == {
        "ids": {"tmdb": 99},
        "seasons": [{"number": 1, "episodes": [{"number": 44, "watched_at": original}]}],
    }
    assert (await engine.status(0))["failed"] == 0
    assert "do-not-store" not in json.dumps(list(store.data.values()))


async def test_deleted_item_is_retired_without_any_trakt_request(harness, episode):
    engine, _, clock, requests = await fail_episode(harness, episode)
    engine.resolver = MetadataResolver(metadata_ops(deleted=True))
    clock[0] += 600
    await engine.drain(0)
    assert len(requests) == 1
    assert (await engine.status(0))["failed"] == 0
    assert (await engine.activity(0))["records"][0]["state"] == "skipped"


async def test_missing_file_waits_for_later_repair_without_writes(harness, episode):
    engine, _, clock, requests = await fail_episode(harness, episode)
    engine.resolver = MetadataResolver(metadata_ops(missing=True))
    clock[0] += 600
    await engine.drain(0)
    assert len(requests) == 1
    ledger = await engine.ledger(0)
    assert ledger["failed"][0]["retry_at"] > clock[0]
    assert not ledger["queue"]


async def test_legacy_manual_correction_checks_remote_page_two_and_skips_duplicate(
    harness, episode
):
    engine, _, clock, _ = await fail_episode(harness, episode)
    ledger = await engine.ledger(0)
    ledger["failed"][0].pop("source")
    await engine.save_ledger(0, ledger)
    calls = []

    def dispatch(request):
        calls.append(request)
        assert request.method == "GET"
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json=[
                {
                    "show": {"ids": {"tmdb": 9}},
                    "episode": {"season": 1, "number": 1},
                    "watched_at": iso_time(clock[0]),
                }
            ]
            if page == 2
            else [{}] * 100,
        )

    engine.client.http._transport = httpx.MockTransport(dispatch)
    await engine.retry_failed(0, "failed", {"season": 1, "episode": 1})
    await engine.drain(0)
    assert len(calls) == 2
    row = (await engine.activity(0))["records"][0]
    assert row["season"] == 1 and row["episode"] == 1
    assert "未重复添加" in row["reason"]


async def test_migration_retires_only_old_non_completions(harness, episode):
    engine, store, clock, _ = await fail_episode(harness, episode)
    ledger = await engine.ledger(0)
    for n in range(15):
        job = copy.deepcopy(ledger["failed"][0])
        job.update(id=f"heartbeat-{n}", path="/scrobble/start", body={"progress": 30})
        ledger["failed"].append(job)
    await engine.save_ledger(0, ledger)
    clock[0] += 3600
    await engine.initialize()
    assert len((await engine.ledger(0))["failed"]) == 1
    assert (await engine.ledger(0))["failed"][0]["id"] == "failed"
    restarted = Engine(store, engine.client, engine.health, engine.log, clock=engine.clock)
    assert (await restarted.ledger(0))["failed"][0]["retry_at"]


async def test_host_error_during_sending_preserves_job_and_uses_backoff(harness, episode):
    engine, _, clock, requests = await fail_episode(harness, episode)
    engine.resolver = MetadataResolver(metadata_ops())
    await engine.retry_failed(0, "failed")
    engine.resolver = AsyncMock(side_effect=OpsError(503, "UNAVAILABLE", "fixture"))
    await engine.drain(0)
    ledger = await engine.ledger(0)
    assert len(ledger["queue"]) == 1 and len(requests) == 1
    assert ledger["queue"][0]["next_at"] == clock[0] + 600


async def test_due_failure_cannot_override_queue_backoff_marker(harness, episode):
    engine, _, clock, _ = await fail_episode(harness, episode)
    ledger = await engine.ledger(0)
    job = copy.deepcopy(ledger["failed"][0])
    job["next_at"] = clock[0] + 3600
    ledger["queue"] = [job]
    ledger["failed"][0]["retry_at"] = clock[0] - 1
    await engine.save_ledger(0, ledger)
    assert (await engine.get(0, "delivery"))["next_at"] == clock[0] + 3600


async def test_dismiss_is_scoped_and_does_not_delete_remote_history(harness, episode):
    engine, _, _, requests = await fail_episode(harness, episode)
    with pytest.raises(ValueError):
        await engine.dismiss_failed(1, "failed")
    assert (await engine.status(0))["failed"] == 1
    assert (await engine.dismiss_failed(0, "failed"))["dismissed"] == 1
    assert len(requests) == 1
    assert not (await engine.status(0))["failed"]
