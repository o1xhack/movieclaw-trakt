"""市场自动提交的离线回归：只使用临时 Git 仓库与模拟 GitHub，不创建远端 PR。"""

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location(
    "marketplace_sync", Path(__file__).resolve().parents[1] / "scripts/marketplace_sync.py"
)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def init(root):
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")


def commit(root):
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


def release(tag):
    return {"tag_name": tag, "draft": False, "prerelease": False, "published_at": "2026-10-21"}


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    init(root)
    for name in ["README.md", "README.en.md", "LICENSE", "PRIVACY.md", "CHANGELOG.md"]:
        (root / name).write_text("fixture\n")
    for folder in ["marketplace/tests", "trakt_sync", "web"]:
        (root / folder).mkdir(parents=True)
    (root / "trakt_sync/__init__.py").write_text("# fixture\n")
    (root / "web/index.html").write_text("fixture\n")
    (root / "marketplace/tests/test_smoke.py").write_text("# fixture\n")
    (root / "marketplace/store.json").write_text('{"author":"fixture"}\n')
    (root / "marketplace/release.toml").write_text(
        'enabled = false\nmovieclaw_release = "v0.41.0"\n'
    )
    (root / "movieclaw-plugin.toml").write_text('[plugin]\nid = "trakt-sync"\nversion = "0.3.1"\n')
    (root / "pyproject.toml").write_text('[project]\nversion = "0.3.1"\n')
    commit(root)
    return root


@pytest.fixture
def github(source):
    def api(path, body=None):
        assert body is None, "prepare must never write to GitHub"
        if "/releases/tags/" in path:
            return release(path.rsplit("/", 1)[1])
        if path == f"repos/{sync.SOURCE}/commits/v0.3.1":
            return {"sha": git(source, "rev-parse", "HEAD")}
        if path == f"repos/{sync.HOST}/commits/v0.41.0":
            return {"sha": "a" * 40}
        if path == f"repos/{sync.MARKET}/commits/main":
            return {"sha": "b" * 40}
        raise AssertionError(path)

    with (
        patch.object(sync, "api", side_effect=api),
        patch.object(sync, "content", return_value="  MOVIECLAW_REF: " + "c" * 40 + "\n"),
    ):
        yield


@pytest.mark.parametrize("tag", ["v0.3.1-beta.1", "v0.3.1-rc.1", "v0.3.1+build", "main", "v01.2.3"])
def test_reject_nonstable_tags(tag):
    with pytest.raises(ValueError):
        sync.stable_release(release(tag), tag)


@pytest.mark.parametrize(
    "change",
    [
        {"draft": True},
        {"prerelease": True},
        {"published_at": None},
        {"tag_name": "v9.0.0"},
        {"draft": None},
    ],
)
def test_release_flags_are_required_even_for_unsuffixed_version(change):
    with pytest.raises(ValueError):
        sync.stable_release({**release("v0.2.1"), **change}, "v0.2.1")


def test_preview_is_readonly_allowlisted_and_disabled_by_default(source, tmp_path, github):
    # A committed credential file outside the release allowlist must never be copied.
    (source / "private-settings.json").write_text("private fixture")
    commit(source)
    output = tmp_path / "candidate"
    plan = sync.prepare(source, output, "v0.3.1")
    assert not plan["enabled"] and plan["host_sha"] == "a" * 40
    assert plan["market_pin"] == "c" * 40
    assert not (output / "plugin/private-settings.json").exists()
    assert not (output / "plugin/release.toml").exists()
    assert (output / "plugin/tests/test_smoke.py").is_file()
    assert sync.hashes(output / "plugin") == plan["files"]
    assert json.loads((output / "plugin/SOURCE.json").read_text()) == sync.provenance(plan)
    with pytest.raises(ValueError, match="未启用"):
        sync.prepare(source, tmp_path / "other", "v0.3.1", apply=True)


def test_dirty_checkout_fails_before_materializing_candidate(source, tmp_path, github):
    (source / "README.md").write_text("changed")
    with pytest.raises(ValueError, match="未提交"):
        sync.prepare(source, tmp_path / "candidate", "v0.3.1")
    assert not (tmp_path / "candidate").exists()


def test_moved_source_tag_and_diverging_market_pins_fail_closed(source, tmp_path, github):
    original = sync.api

    def moved(path, body=None):
        if path == f"repos/{sync.SOURCE}/commits/v0.3.1":
            return {"sha": "d" * 40}
        return original(path, body)

    with patch.object(sync, "api", side_effect=moved):
        with pytest.raises(ValueError, match="确切提交"):
            sync.prepare(source, tmp_path / "moved", "v0.3.1")
    with patch.object(
        sync,
        "content",
        side_effect=[
            "  MOVIECLAW_REF: " + "c" * 40,
            "  MOVIECLAW_REF: " + "d" * 40,
        ],
    ):
        with pytest.raises(ValueError, match="基线不一致"):
            sync.prepare(source, tmp_path / "pin", "v0.3.1")


def test_symlink_and_missing_files_rejected(source):
    (source / "web/secret.py").symlink_to(source / "README.md")
    with pytest.raises(ValueError):
        sync.files(source)
    (source / "web/secret.py").unlink()
    (source / "LICENSE").unlink()
    with pytest.raises(ValueError):
        sync.files(source)


def test_market_pin_format_changes_fail_closed():
    with pytest.raises(ValueError):
        sync.market_pin("  MOVIECLAW_REF: main\n")


def test_submit_checks_fingerprints_and_recovers_after_push_before_pr_failure(
    source, tmp_path, github, monkeypatch
):
    (source / "marketplace/release.toml").write_text(
        'enabled = true\nmovieclaw_release = "v0.41.0"\n'
    )
    commit(source)
    candidate = tmp_path / "candidate"
    plan = sync.prepare(source, candidate, "v0.3.1", apply=True)
    market = tmp_path / "market"
    init(market)
    (market / "index.json").write_text("{}\n")
    base = commit(market)
    plan["market_sha"] = base
    (candidate / "plan.json").write_text(json.dumps(plan))
    (candidate / "index.json").write_text('{"fixture":true}\n')
    (candidate / "validated.json").write_text(json.dumps(sync.validation(candidate)))
    fork = tmp_path / "fork.git"
    subprocess.check_call(["git", "init", "--bare", str(fork)])
    monkeypatch.setenv("GH_TOKEN", "fixture-not-a-real-token")
    real_run = sync.run
    writes = []
    fail = [True]
    opened = []

    def api(path, body=None):
        if body is not None:
            writes.append(body)
            if fail[0]:
                fail[0] = False
                raise ValueError("fixture transient PR creation failure")
            opened.append(
                {
                    "state": "open",
                    "head": {"repo": {"full_name": sync.FORK}},
                    "html_url": "https://example.invalid/pr/1",
                }
            )
            return {"html_url": "https://example.invalid/pr/1"}
        if path.endswith("/releases/tags/v0.3.1"):
            return release("v0.3.1")
        if path.endswith("/releases/tags/v0.41.0"):
            return release("v0.41.0")
        if "/commits/" in path:
            return {
                "sha": {
                    sync.SOURCE: plan["source_sha"],
                    sync.HOST: plan["host_sha"],
                    sync.MARKET: base,
                }[path.split("repos/", 1)[1].split("/commits/")[0]]
            }
        assert "/pulls?" in path
        return opened

    def local_run(*args, **kwargs):
        args = tuple(str(fork) if a == f"https://github.com/{sync.FORK}.git" else a for a in args)
        return real_run(*args, **kwargs)

    with (
        patch.object(sync, "api", side_effect=api),
        patch.object(sync, "run", side_effect=local_run),
    ):
        with pytest.raises(ValueError, match="fixture transient"):
            sync.submit(candidate, market)
        # Use a clean market checkout like a fresh workflow runner.
        git(market, "switch", "main")
        sync.submit(candidate, market)
        assert len(writes) == 2
        assert writes[0] == writes[1]
        assert git(fork, "rev-list", "--count", "sync-trakt-sync-v0.3.1") == "2"
        sync.submit(candidate, market)
        assert len(writes) == 2  # Retry after success reuses the PR without pushing or creating.
        (candidate / "index.json").write_text('{"unexpected":true}\n')
        with pytest.raises(ValueError, match="检查后"):
            sync.submit(candidate, market)
        (candidate / "validated.json").write_text(json.dumps(sync.validation(candidate)))
        with pytest.raises(ValueError, match="索引与本次检查不一致"):
            sync.submit(candidate, market)
        (candidate / "plugin/README.md").write_text("tampered")
        with pytest.raises(ValueError, match="候选文件"):
            sync.submit(candidate, market)
        assert len(writes) == 2
