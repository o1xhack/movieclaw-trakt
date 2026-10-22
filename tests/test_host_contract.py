"""隔离 SQLite / 真实插件子进程的旧接口边界探测；不连接用户服务器。

额外依赖宿主运行环境，显式 MOVIECLAW_HOST_QA=1 时执行。
"""

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("MOVIECLAW_HOST_QA") != "1", reason="显式启用隔离宿主验收"
)


@pytest.fixture
def host(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from movieclaw_api.core.config import get_settings
    from movieclaw_api.services import durable_events
    from movieclaw_api.services.auth import reset_auth_state
    from movieclaw_api.settings.store import reset_setting_store
    from movieclaw_db.crypto import reset_secret_box

    for key, value in {
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'probe.db'}",
        "SECRET_KEY_FILE": str(tmp_path / ".secret_key"),
        "SITE_CONFIGS_DIR": str(tmp_path / "site-configs"),
        "MOVIECLAW_DATA_DIR": str(tmp_path),
        "MEDIA_DIR": str(tmp_path / "media"),
        "LOG_DIR": str(tmp_path / "logs"),
        "SCHEDULER_ENABLED": "false",
        "TMDB_API_KEY": "",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    durable_events.reset_state()
    (tmp_path / "plugins").mkdir()
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "trakt_sync",
        tmp_path / "plugins/trakt_sync",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (tmp_path / "plugins.yaml").write_text(
        "- id: trakt-sync\n  module: trakt_sync\n  local: true\n  runtime: process\n"
    )
    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as client:
        from movieclaw_api.api.deps import require_login
        from movieclaw_api.services.auth import Principal

        viewer = {"member": None}

        def login_as(member):
            viewer["member"] = member
            if member is None:
                app.dependency_overrides.pop(require_login, None)
            else:
                app.dependency_overrides[require_login] = lambda: Principal(
                    kind="admin" if viewer["member"] == 0 else "member",
                    name="fixture",
                    member_id=viewer["member"] or None,
                    is_admin=viewer["member"] == 0,
                )

        yield app, client, login_as, tmp_path
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()
    durable_events.reset_state()


async def seed(root):
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import FileSource, FileState, LibraryFile, MediaItem, Member
    from movieclaw_db.repositories.library_repo import LibraryRepository

    root.mkdir()
    f = root / "movie.mkv"
    f.write_bytes(b"fixture")
    async with get_database().session() as s:
        library = await LibraryRepository(s).create(
            name="隔离电影库", kind="movie", root_paths=[str(root)]
        )
        movie = MediaItem(kind="movie", tmdb_id=11, title="隔离电影", original_title="Fixture")
        s.add(movie)
        s.add_all(
            [
                Member(id=1, username="one", password_hash="unused"),
                Member(id=2, username="two", password_hash="unused"),
            ]
        )
        await s.flush()
        s.add(
            LibraryFile(
                library_id=library.id,
                media_item_id=movie.id,
                season_number=0,
                episode_number=0,
                file_path=str(f),
                size_bytes=7,
                source=FileSource.SCANNED,
                state=FileState.IN_PLACE,
                duration_seconds=600,
            )
        )
        await s.commit()
        return movie.id


async def facts(item):
    from movieclaw_db.engine import get_database
    from movieclaw_db.models import PlaybackLog, PlaybackState
    from sqlalchemy import select

    async with get_database().session() as s:
        states = (
            (await s.execute(select(PlaybackState).where(PlaybackState.media_item_id == item)))
            .scalars()
            .all()
        )
        logs = (
            (await s.execute(select(PlaybackLog).where(PlaybackLog.media_item_id == item)))
            .scalars()
            .all()
        )
        return {
            r.member_id: {"played": r.played, "count": r.play_count, "when": r.last_played_at}
            for r in states
        }, logs


def test_real_process_boot_with_original_routes_and_admin_boundary(host):
    app, c, login, _ = host
    from movieclaw_api.services import plugin_runtime

    assert "trakt-sync" in plugin_runtime.process_entries
    path = "/api/v1/plugins/trakt-sync"
    assert c.get(path + "/profile").status_code == 401
    login(0)
    response = c.get(path + "/profile?member_id=2")
    assert response.status_code == 200, response.text
    prefs = {"manual_watched": True, "manual_unwatched": False, "favorites": True}
    assert c.put(path + "/profile?member_id=2", json=prefs).status_code == 200
    assert c.get(path + "/profile?member_id=2").json()["preferences"] == prefs
    assert c.get(path + "/profile?member_id=0").json()["preferences"] != prefs
    assert c.get(path + "/history").json()["tasks"] == []
    assert c.post(path + "/history", json={"direction": "invalid"}).status_code == 409
    login(2)
    assert (
        c.get(path + "/profile?member_id=2", headers={"X-MovieClaw-Member": "0"}).status_code == 403
    )
    assert c.put(path + "/profile?member_id=0", json=prefs).status_code == 403


def test_marks_are_current_identity_only_and_playback_log_is_not_complete_state(host):
    _, c, login, root = host
    item = c.portal.call(seed, root / "media-fixture")
    login(1)
    r = c.post(
        "/api/v1/playback/marks",
        json={
            "media_item_id": item,
            "played": True,
            "member_id": 2,
            "watched_at": "2020-01-01T00:00:00Z",
        },
    )
    assert r.status_code == 200, r.text
    states, logs = c.portal.call(facts, item)
    assert states[1]["played"] and 2 not in states
    assert not logs
    assert states[1]["when"] is None or states[1]["when"].year != 2020
    login(2)
    assert not c.get(
        "/api/v1/playback/marks", params={"media_item_id": item, "member_id": 1}
    ).json()["data"]["played"]
    assert c.get("/api/v1/playback/history?member_id=1").status_code == 403
    login(0)
    r = c.get("/api/v1/playback/history?member_id=1")
    assert r.status_code == 200 and r.json()["data"]["entries"] == []


def test_offline_viewings_can_preserve_date_but_add_a_play_and_log(host):
    _, c, login, root = host
    item = c.portal.call(seed, root / "media-fixture")
    login(2)
    now = datetime.now(timezone.utc)
    when = now - timedelta(days=365)
    body = {
        "sent_at": now.isoformat(),
        "records": [
            {
                "viewing_id": "contract-probe-viewing",
                "media_item_id": item,
                "started_at": when.isoformat(),
                "updated_at": when.isoformat(),
                "position_ms": 600000,
                "watched_ms": 0,
                "completed": True,
                "ended": True,
            }
        ],
    }
    r = c.post("/api/v1/playback/viewings", json=body)
    assert r.status_code == 200 and r.json()["data"]["results"][0]["status"] == "applied", r.text
    states, logs = c.portal.call(facts, item)
    assert states[2]["played"] and states[2]["count"] == 1
    assert len(logs) == 1 and logs[0].completed
    assert abs((states[2]["when"].replace(tzinfo=timezone.utc) - when).total_seconds()) < 10
    c.post("/api/v1/playback/viewings", json=body)
    states, logs = c.portal.call(facts, item)
    assert states[2]["count"] == 1 and len(logs) == 1


def test_original_plugin_contract_has_no_caller_or_background_mark_operations():
    from movieclaw_api.services.host_ops import _operation_index
    from movieclaw_sdk.services import RoutesApi

    assert not hasattr(RoutesApi, "caller")
    index = _operation_index()
    assert "playback.history" in index
    for operation in (
        "playback.marks.get",
        "playback.marks.set",
        "playback.viewings",
        "playback.sync.snapshot",
        "playback.sync.apply",
    ):
        assert operation not in index


def test_explicit_device_pairing_is_a_separate_member_delegation_route(host):
    """额外用户批准能取得成员权限；这是不同授权方案，不能冒充现有插件权限。"""
    _, c, login, root = host
    item = c.portal.call(seed, root / "delegation-media")
    login(None)
    r = c.post(
        "/api/v1/auth/device/authorize",
        json={"client_type": "cli", "client_name": "Trakt sync local contract probe"},
    )
    assert r.status_code == 200
    challenge = r.json()["data"]
    login(2)  # 仅在隔离测试中模拟用户批准；不在真实服务器批准。
    assert (
        c.post("/api/v1/auth/devices/requests/" + challenge["user_code"] + "/approve").status_code
        == 200
    )
    login(None)
    r = c.post("/api/v1/auth/device/token", json={"device_code": challenge["device_code"]})
    assert r.status_code == 200
    headers = {"Authorization": "Bearer " + r.json()["data"]["token"]}
    r = c.post(
        "/api/v1/playback/marks", headers=headers, json={"media_item_id": item, "played": True}
    )
    assert r.status_code == 200
    states, _ = c.portal.call(facts, item)
    assert states[2]["played"] and 0 not in states
    assert c.get("/api/v1/playback/history", headers=headers).status_code == 403
    # 设备令牌可调用成员权限内的其他接口；不是只限本插件的最小授权。
    assert c.get("/api/v1/auth/me", headers=headers).status_code == 200


def test_first_manual_mark_has_click_time_but_repeat_keeps_previous_time(host, monkeypatch):
    from movieclaw_playback import progress

    _, c, login, root = host
    item = c.portal.call(seed, root / "manual-clock-media")
    login(0)
    clock = [datetime(2026, 10, 21, 18, 0, 0)]  # 洛杉矶 10/21 11:00
    monkeypatch.setattr(progress, "utcnow", lambda: clock[0])
    assert (
        c.post("/api/v1/playback/marks", json={"media_item_id": item, "played": True}).status_code
        == 200
    )
    states, logs = c.portal.call(facts, item)
    assert states[0]["when"] == clock[0]
    assert states[0]["count"] == 1 and not logs
    clock[0] += timedelta(hours=1)
    c.post("/api/v1/playback/marks", json={"media_item_id": item, "played": True})
    states, logs = c.portal.call(facts, item)
    assert states[0]["when"] == datetime(2026, 10, 21, 18, 0, 0)
    assert states[0]["count"] == 1 and not logs


def test_three_actual_plays_have_three_logs_but_completed_flag_is_not_per_rewatch(
    host, monkeypatch
):
    from movieclaw_api.services.playback import watch
    from movieclaw_playback import state

    _, c, login, root = host
    item = c.portal.call(seed, root / "rewatch-media")
    login(0)
    clock = [datetime(2026, 10, 19, 18, 0, 0)]
    monkeypatch.setattr(watch, "utcnow", lambda: clock[0])
    monkeypatch.setattr(state, "utcnow", lambda: clock[0])
    for _ in range(3):
        base = {"media_item_id": item, "device_id": "rewatch-contract-device"}
        for event, position in [("start", 0), ("progress", 240000), ("stop", 570000)]:
            r = c.post(
                "/api/v1/playback/progress", json={**base, "event": event, "position_ms": position}
            )
            assert r.status_code == 200
            clock[0] += timedelta(minutes=5)
        clock[0] += timedelta(days=1)
    states, logs = c.portal.call(facts, item)
    assert len(logs) == 3 and states[0]["count"] == 3
    logs.sort(key=lambda row: row.started_at)
    assert [row.end_position_ms for row in logs] == [570000] * 3
    assert [row.completed for row in logs] == [True, False, False]
    r = c.get("/api/v1/playback/history?member_id=0")
    assert r.status_code == 200
    rows = r.json()["data"]["entries"]
    assert len(rows) == 3
    assert all(row["duration_ms"] == 600000 and row["progress_percent"] == 95 for row in rows)
