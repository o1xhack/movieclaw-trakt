"""Import identity must survive failed profile lookup, rebinding and username changes."""

import httpx
import pytest

from trakt_sync.transfers import Transfers, digest

from .test_transfers import Ops, drive, remote, unit

UUID = "fixture-stable-user-uuid"


def api(profile):
    def handler(r):
        if r.url.path == "/oauth/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "fixture-device",
                    "user_code": "TEST",
                    "verification_url": "https://trakt.tv/activate",
                    "interval": 1,
                    "expires_in": 600,
                },
            )
        if r.url.path == "/oauth/device/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "fixture-access",
                    "refresh_token": "fixture-refresh",
                    "expires_in": 3600,
                },
            )
        if r.url.path == "/users/settings":
            return httpx.Response(
                profile.get("status", 200),
                json=profile.get(
                    "body", {"user": {"username": "renamed-user", "ids": {"uuid": UUID}}}
                ),
            )
        if r.url.path == "/sync/last_activities":
            return httpx.Response(200, json={"movies": {"watched_at": "2020-01-02T00:00:00Z"}})
        if r.url.path in {"/sync/history/movies", "/sync/history/episodes"}:
            kind = "movie" if r.url.path.endswith("movies") else "tv"
            return httpx.Response(
                200,
                json=[remote(1, episode=1 if kind == "tv" else None)],
                headers={"X-Pagination-Page-Count": "1"},
            )

    return handler


async def connect(e):
    await e.begin(0)
    assert (await e.poll(0))["state"] == "connected"


async def import_all(e, ops, clock):
    t = Transfers(e, ops)
    task = await drive(t, await t.create(0, "import"), "ready", clock)
    await t.confirm(0, task["id"], task["preview_token"])
    return await drive(t, task, "complete", clock)


def update_catalog_version(ops):
    for u in ops.units:
        u["updated_at"] = ops.versions.get((u["media"]["id"], u["season"], u["episode"]))


@pytest.mark.parametrize("kind", ["movie", "tv"])
async def test_profile_failure_then_rebinding_same_account_does_not_reimport(harness, kind):
    profile = {"status": 503}
    e, store, clock, requests = await harness(api(profile))
    await store.delete("account", scope="member:0")
    await connect(e)  # OAuth succeeds even though optional display profile lookup fails.
    assert (await e.get(0, "account"))["username"] is None
    profile["status"] = 200
    ops = Ops([unit(kind=kind, season=int(kind == "tv"), episode=int(kind == "tv"), played=False)])
    await import_all(e, ops, clock)
    assert len(ops.writes) == 1
    update_catalog_version(ops)
    await e.disconnect(0)
    await connect(e)
    await import_all(e, ops, clock)
    assert len(ops.writes) == 1  # actual host also checks source ID before state version
    assert (await e.get(0, "account"))["user_uuid"] == UUID
    assert ("member:0", "account") in store.secrets
    assert not [r for r in requests if r.method == "POST" and r.url.path.startswith("/sync/")]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"user": {"username": "name-only"}},
        {"user": {"ids": {"uuid": " "}}},
        {"user": {"ids": {"uuid": 42}}},
    ],
)
async def test_missing_stable_identity_pauses_without_reading_or_writing_host(harness, body):
    e, _, _, _ = await harness(api({"body": body}))
    account = await e.get(0, "account")
    account.update(username=None, identity_version=1)
    await e.set(0, "account", account, secret=True)
    ops = Ops([unit(played=False)])
    t = Transfers(e, ops)
    task = await t.create(0, "import")
    task = await t.step(task["id"], 0)
    assert task["state"] == "paused" and "账号" in task["error"]
    assert not ops.calls


@pytest.mark.parametrize("status", [429, 503])
async def test_identity_fetch_retries_with_backoff_and_recovers(harness, status):
    profile = {"status": status}
    e, _, clock, requests = await harness(api(profile))
    ops = Ops([unit(played=False)])
    t = Transfers(e, ops)
    task = await t.create(0, "import")
    task = await t.step(task["id"], 0)
    assert task["state"] == "scanning" and task["next_at"] > clock[0]
    assert not ops.calls and [r.url.path for r in requests] == ["/users/settings"]
    profile["status"] = 200
    task = await drive(t, task, "ready", clock)
    assert task["total"] == 1
    assert (await e.get(0, "account"))["user_uuid"] == UUID


@pytest.mark.parametrize("username", ["legacy-name", None])
async def test_legacy_import_namespace_survives_uuid_migration_rebinding_and_rename(
    harness, username
):
    profile = {}
    e, _, clock, _ = await harness(api(profile))
    account = await e.get(0, "account")
    account["username"] = username
    await e.set(0, "account", account, secret=True)
    ops = Ops([unit(played=False)])
    legacy_id = digest([username or account["generation"], 1])
    await ops.call(
        "playback.sync.import",
        {
            "records": [
                {
                    "media_item_id": 1,
                    "season": 0,
                    "episode": 0,
                    "source_id": legacy_id,
                    "watched_at": "2020-01-01T00:00:00Z",
                    "expected_updated_at": None,
                }
            ]
        },
    )
    update_catalog_version(ops)
    await import_all(e, ops, clock)
    assert len(ops.writes) == 1
    await e.disconnect(0)
    await connect(e)  # same UUID, different username and a new generation
    await import_all(e, ops, clock)
    assert len(ops.writes) == 1
    # A different account never inherits the previous account's namespace.
    await e.disconnect(0)
    profile["body"] = {
        "user": {"username": "renamed-user", "ids": {"uuid": "other-stable-user-uuid"}}
    }
    await connect(e)
    await import_all(e, ops, clock)
    assert len(ops.writes) == 2


@pytest.mark.parametrize("state", ["ready", "running"])
async def test_legacy_inflight_import_requires_new_preview_before_host_write(harness, state):
    e, _, clock, _ = await harness(api({}))
    ops = Ops([unit(played=False)])
    t = Transfers(e, ops)
    task = await drive(t, await t.create(0, "import"), "ready", clock)
    task.pop("import_identity", None)
    task.pop("import_schema", None)
    task["state"] = state
    await t.save(task)
    if state == "ready":
        with pytest.raises(ValueError, match="重新扫描"):
            await t.confirm(0, task["id"], task["preview_token"])
    else:
        task = await t.step(task["id"], 0)
        assert task["state"] == "paused" and "重新扫描" in task["error"]
    assert not ops.writes


@pytest.mark.parametrize("where", ["registry", "account"])
async def test_interrupted_uuid_migration_retains_legacy_namespace(harness, monkeypatch, where):
    e, store, clock, _ = await harness(api({}))
    account = await e.get(0, "account")
    account["username"] = "legacy-before-crash"
    await e.set(0, "account", account, secret=True)
    original = store.set
    failures = []

    async def interrupted(key, value, **kwargs):
        if not failures and (
            (where == "registry" and key.startswith("import_identity_"))
            or (where == "account" and key == "account")
        ):
            failures.append(key)
            raise OSError("identity persistence interrupted")
        return await original(key, value, **kwargs)

    monkeypatch.setattr(store, "set", interrupted)
    ops = Ops([unit(played=False)])
    t = Transfers(e, ops)
    task = await t.create(0, "import")
    task = await t.step(task["id"], 0)
    assert failures and not ops.calls and task["next_at"] > clock[0]
    assert not (await e.get(0, "account")).get("user_uuid")
    t = Transfers(e, ops)
    task = await drive(t, task, "ready", clock)
    row = (await t.details(0, task["id"], records=True))["items"][0]
    assert row["source_id"] == digest(["legacy-before-crash", 1])
    assert (await e.get(0, "account"))["user_uuid"] == UUID


async def test_uuid_lookup_preserves_rotated_tokens(harness):
    base = api({})

    def handler(r):
        if r.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                },
            )
        if r.url.path == "/users/settings":
            assert r.headers["Authorization"] == "Bearer rotated-access"
        return base(r)

    e, _, clock, _ = await harness(handler)
    account = await e.get(0, "account")
    account["expires_at"] = clock[0] - 1
    await e.set(0, "account", account, secret=True)
    t = Transfers(e, Ops([unit(played=False)]))
    await drive(t, await t.create(0, "import"), "ready", clock)
    saved = await e.get(0, "account")
    assert saved["user_uuid"] == UUID
    assert saved["access_token"] == "rotated-access" and saved["refresh_token"] == "rotated-refresh"


@pytest.mark.parametrize(
    "user", [None, {"username": "name", "ids": None}, {"username": "name", "ids": {"uuid": UUID}}]
)
async def test_authorization_keeps_tokens_and_privately_caches_valid_uuid(harness, user):
    e, store, _, _ = await harness(api({"body": {"user": user}}))
    await store.delete("account", scope="member:0")
    await connect(e)
    account = await e.get(0, "account")
    assert account["identity_version"] == 1 and account["access_token"] == "fixture-access"
    assert account.get("user_uuid") == (UUID if user and user.get("ids") else None)
    assert UUID not in str(await e.status(0))


@pytest.mark.parametrize("kind", ["movie", "tv"])
@pytest.mark.parametrize("username", ["legacy-user", None])
async def test_disconnect_migrates_beta_history_before_rebinding_without_another_scan(
    harness, kind, username
):
    e, _, clock, _ = await harness(api({}))
    account = await e.get(0, "account")
    account["username"] = username
    await e.set(0, "account", account, secret=True)
    await e.set(0, "import_stamp", {"generation": account["generation"]})
    u = unit(kind=kind, season=int(kind == "tv"), episode=int(kind == "tv"), played=False)
    ops = Ops([u])
    await ops.call(
        "playback.sync.import",
        {
            "records": [
                {
                    "media_item_id": 1,
                    "season": u["season"],
                    "episode": u["episode"],
                    "source_id": digest([username or account["generation"], 1]),
                    "watched_at": "2020-01-01T00:00:00Z",
                    "expected_updated_at": None,
                }
            ]
        },
    )
    update_catalog_version(ops)
    # Upgrade, then disconnect/reconnect immediately. No post-upgrade scan first.
    assert (await e.disconnect(0))["disconnected"]
    await connect(e)
    await import_all(e, ops, clock)
    assert len(ops.writes) == 1


@pytest.mark.parametrize("failure", ["network", "storage"])
async def test_disconnect_retains_beta_binding_until_identity_is_durable(
    harness, monkeypatch, failure
):
    profile = {"status": 503 if failure == "network" else 200}
    e, store, _, requests = await harness(api(profile))
    account = await e.get(0, "account")
    account["username"] = "legacy-retained"
    await e.set(0, "account", account, secret=True)
    # Partial legacy imports can lack the completed import_stamp.
    await store.set(
        "task",
        {
            "id": "old-partial",
            "member_id": 0,
            "direction": "import",
            "generation": account["generation"],
            "confirmed_at": "2020-01-01T00:00:00Z",
            "state": "paused",
            "total": 3,
        },
        scope="history:old-partial",
    )
    original = store.set
    interrupted = []

    async def persist(key, value, **kwargs):
        if failure == "storage" and not interrupted and key.startswith("import_identity_"):
            interrupted.append(key)
            raise OSError("registry unavailable")
        return await original(key, value, **kwargs)

    monkeypatch.setattr(store, "set", persist)
    result = await e.disconnect(0)
    assert not result["disconnected"] and result["import_identity_pending"]
    assert await e.get(0, "account") == account
    assert not [r for r in requests if r.url.path == "/oauth/revoke"]
    profile["status"] = 200
    assert (await e.disconnect(0))["disconnected"]
    assert not await e.get(0, "account")


async def test_only_explicit_reset_allows_disconnect_with_unresolved_legacy_identity(harness):
    e, _, _, requests = await harness(api({"status": 401}))
    account = await e.get(0, "account")
    await e.set(0, "import_stamp", {"generation": account["generation"]})
    # An unavailable/revoked legacy authorization must not permanently trap the user.
    assert not (await e.disconnect(0))["disconnected"]
    result = await e.disconnect(0, allow_legacy_identity_loss=True)
    assert result["disconnected"] and not await e.get(0, "account")
    assert any(r.url.path == "/oauth/revoke" for r in requests)
