"""发布资格检查、只读候选目录，以及显式启用后的跨仓库 PR；仅用标准库和 gh/git。"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

SOURCE = "o1xhack/movieclaw-trakt"
HOST = "movieclaw/MovieClaw"
MARKET = "movieclaw/movieclaw-plugins"
FORK = "o1xhack/movieclaw-plugins"
PLUGIN = "trakt-sync"
VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
SHA = re.compile(r"[0-9a-f]{40}")


def run(*args, cwd=None, data=None):
    return subprocess.check_output(args, cwd=cwd, input=data, text=True).strip()


def api(path, body=None):
    args = ["gh", "api", path]
    if body is not None:
        args += ["--method", "POST", "--input", "-"]
    return json.loads(run(*args, data=json.dumps(body) if body is not None else None))


def stable_release(release, tag):
    if not isinstance(tag, str) or not re.fullmatch(r"v" + VERSION.pattern, tag):
        raise ValueError("只接受 v主.次.修订 的正式版 tag")
    if (
        release.get("tag_name") != tag
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or not release.get("published_at")
    ):
        raise ValueError(f"{tag} 必须是已发布且不是 draft/prerelease 的 Release")


def market_pin(text):
    values = re.findall(r"^  MOVIECLAW_REF: ([0-9a-f]{40})\s*$", text, re.MULTILINE)
    if len(values) != 1:
        raise ValueError("无法明确读取市场 MOVIECLAW_REF，请检查上游工作流格式")
    return values[0]


def content(repo, ref, path):
    obj = api(f"repos/{repo}/contents/{path}?ref={ref}")
    return base64.b64decode(obj["content"]).decode()


def files(root):
    """明确的发布允许列表；不复制配置、Git、开发脚本、凭据和缓存。"""
    names = [
        "movieclaw-plugin.toml",
        "README.md",
        "README.en.md",
        "LICENSE",
        "PRIVACY.md",
        "CHANGELOG.md",
        "marketplace/store.json",
    ]
    for folder, pattern in [
        ("trakt_sync", "*.py"),
        ("web", "*"),
        ("docs", "*.md"),
        ("marketplace/tests", "*.py"),
    ]:
        base = root / folder
        if base.is_symlink():
            raise ValueError(f"禁止发布软链接：{folder}")
        names += [str(p.relative_to(root)) for p in base.rglob(pattern) if p.is_file()]
    result = {}
    for name in sorted(set(names)):
        path = root / name
        if any(p.startswith(".") or p == "__pycache__" for p in Path(name).parts):
            continue
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root.resolve())
        ):
            raise ValueError(f"发布文件无效：{name}")
        data = path.read_bytes()
        if len(data) > 5_000_000:
            raise ValueError(f"发布文件过大：{name}")
        target = name.removeprefix("marketplace/")
        result[target] = data
    if not any(p.startswith("tests/") for p in result):
        raise ValueError("缺少市场 unittest 回归测试")
    if sum(map(len, result.values())) > 50_000_000:
        raise ValueError("发布目录过大")
    return result


def hashes(root):
    result = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("候选目录含软链接")
        if path.is_file() and "__pycache__" not in path.parts:
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def provenance(plan):
    return {k: plan[k] for k in ("version", "tag", "source_sha", "host_tag", "host_sha")}


def branch_hashes(market, ref):
    prefix = f"plugins/{PLUGIN}/"
    names = run("git", "ls-tree", "-r", "--name-only", ref, "--", prefix, cwd=market)
    return {
        name.removeprefix(prefix): hashlib.sha256(
            subprocess.check_output(
                ["git", "show", f"{ref}:{name}"],
                cwd=market,
            )
        ).hexdigest()
        for name in names.splitlines()
    }


def prepare(source, output, tag, *, apply=False):
    config = tomllib.loads((source / "marketplace/release.toml").read_text())
    if type(config.get("enabled")) is not bool:
        raise ValueError("enabled 必须是布尔值")
    if apply and not config["enabled"]:
        raise ValueError("自动提交未启用；先使用 dry-run 验证")
    host_tag = config["movieclaw_release"]
    stable_release(api(f"repos/{SOURCE}/releases/tags/{tag}"), tag)
    stable_release(api(f"repos/{HOST}/releases/tags/{host_tag}"), host_tag)
    manifest = tomllib.loads((source / "movieclaw-plugin.toml").read_text())
    version = tag.removeprefix("v")
    if manifest["plugin"]["id"] != PLUGIN or manifest["plugin"]["version"] != version:
        raise ValueError("插件清单的 id/version 与发布 tag 不符")
    project = tomllib.loads((source / "pyproject.toml").read_text())
    if project["project"]["version"] != version:
        raise ValueError("pyproject 版本与发布 tag 不符")
    source_sha = run("git", "rev-parse", "HEAD", cwd=source)
    if source_sha != api(f"repos/{SOURCE}/commits/{tag}")["sha"]:
        raise ValueError("工作目录不是发布 tag 的确切提交")
    if run("git", "status", "--porcelain", "--untracked-files=all", cwd=source):
        raise ValueError("发布源码含未提交修改")
    host_sha = api(f"repos/{HOST}/commits/{host_tag}")["sha"]
    market_sha = api(f"repos/{MARKET}/commits/main")["sha"]
    pin = market_pin(content(MARKET, market_sha, ".github/workflows/ci.yml"))
    if market_pin(content(MARKET, market_sha, ".github/workflows/release.yml")) != pin:
        raise ValueError("市场 PR 检查与发版的宿主基线不一致，需先由上游处理")
    for value in [source_sha, host_sha, market_sha, pin]:
        if not SHA.fullmatch(value):
            raise ValueError("提交标识无效")
    if output.exists():
        raise ValueError("输出目录已存在，请使用新的候选目录")
    payload = files(source)
    plan = {
        "version": version,
        "tag": tag,
        "enabled": config["enabled"],
        "source_sha": source_sha,
        "host_tag": host_tag,
        "host_sha": host_sha,
        "market_sha": market_sha,
        "market_pin": pin,
    }
    payload["SOURCE.json"] = (json.dumps(provenance(plan), indent=2) + "\n").encode()
    target = output / "plugin"
    for name, data in payload.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    plan["files"] = hashes(target)
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    body = (
        f"Sync {PLUGIN} {version} from https://github.com/{SOURCE}/releases/tag/{tag}.\n\n"
        f"Source commit: `{source_sha}`. Declared MovieClaw Server release: `{host_tag}` "
        f"(`{host_sha}`). Market check pin: `{pin}`.\n\n"
        "Prepared from the release source, with catalog lint, plugin compatibility and unittest "
        "checks against both host commits. No server deployment, catalog release or merge is "
        "performed by this automation. After review/merge, a maintainer can publish "
        f"`{PLUGIN}-v{version}` using the market's normal release workflow.\n"
    )
    (output / "pr-body.md").write_text(body)
    print(json.dumps({k: v for k, v in plan.items() if k != "files"}, indent=2))
    return plan


def submit(candidate, market):
    plan = json.loads((candidate / "plan.json").read_text())
    checked = json.loads((candidate / "validated.json").read_text())
    if checked != validation(candidate):
        raise ValueError("候选或索引在检查后发生变化")
    if plan.get("enabled") is not True:
        raise ValueError("候选发布没有开启自动提交")
    version = plan["version"]
    if not VERSION.fullmatch(version) or hashes(candidate / "plugin") != plan["files"]:
        raise ValueError("候选文件与检查结果不一致")
    # 写入凭据仅在此阶段提供；候选插件代码不会在此阶段执行。
    if not os.environ.get("GH_TOKEN"):
        raise ValueError("需要单独配置 MARKETPLACE_PR_TOKEN")
    stable_release(api(f"repos/{SOURCE}/releases/tags/{plan['tag']}"), plan["tag"])
    stable_release(api(f"repos/{HOST}/releases/tags/{plan['host_tag']}"), plan["host_tag"])
    if api(f"repos/{SOURCE}/commits/{plan['tag']}")["sha"] != plan["source_sha"]:
        raise ValueError("发布 tag 已移动，拒绝提交旧候选")
    if api(f"repos/{HOST}/commits/{plan['host_tag']}")["sha"] != plan["host_sha"]:
        raise ValueError("宿主 tag 已移动，需重新检查")
    if api(f"repos/{MARKET}/commits/main")["sha"] != plan["market_sha"]:
        raise ValueError("市场 main 已变化，请重新演练和检查")
    branch = f"sync-{PLUGIN}-v{version}"
    prs = api(f"repos/{MARKET}/pulls?state=all&head=o1xhack:{branch}&per_page=100")
    if prs:
        if len(prs) != 1 or prs[0]["state"] != "open":
            raise ValueError("此版本已有关闭的 PR，请人工处理")
        if prs[0]["head"]["repo"]["full_name"] != FORK:
            raise ValueError("PR 来源不符")
    fork_url = f"https://github.com/{FORK}.git"
    existing = run("git", "ls-remote", fork_url, f"refs/heads/{branch}", cwd=market)
    if existing:
        run("git", "fetch", fork_url, f"refs/heads/{branch}", cwd=market)
        if branch_hashes(market, "FETCH_HEAD") != plan["files"]:
            raise ValueError("同版本分支已被修改，拒绝覆盖；请人工核对")
        previous_index = json.loads(run("git", "show", "FETCH_HEAD:index.json", cwd=market))
        new_index = json.loads((candidate / "index.json").read_text())
        previous_index.pop("generated_at", None)
        new_index.pop("generated_at", None)
        if previous_index != new_index:
            raise ValueError("已有分支的索引与本次检查不一致，请人工更新或重新基于上游整理")
        changed = run(
            "git", "diff", "--name-only", f"{plan['market_sha']}...FETCH_HEAD", cwd=market
        ).splitlines()
        if any(
            name != "index.json" and not name.startswith(f"plugins/{PLUGIN}/") for name in changed
        ):
            raise ValueError("已有分支包含插件目录以外的修改，请人工核对")
        if prs:
            print(f"已有 PR，不重复创建或覆盖：{prs[0]['html_url']}")
            return
        # 上次推分支成功但创建 PR 失败：沿用已验证的分支，重试创建 PR。
        result = api(
            f"repos/{MARKET}/pulls",
            {
                "title": f"Update {PLUGIN} to {version}",
                "head": f"o1xhack:{branch}",
                "base": "main",
                "body": (candidate / "pr-body.md").read_text(),
            },
        )
        print(result["html_url"])
        return
    if prs:
        raise ValueError("PR 的源分支已不存在，请人工核对")
    target = market / "plugins" / PLUGIN
    if target.exists():
        prior = tomllib.loads((target / "movieclaw-plugin.toml").read_text())["plugin"]["version"]
        if not VERSION.fullmatch(prior):
            raise ValueError("市场当前版本格式需要人工确认")
        if tuple(map(int, version.split("."))) <= tuple(map(int, prior.split("."))):
            print("市场已有相同或较新的版本，跳过提交")
            return
        shutil.rmtree(target)
    shutil.copytree(candidate / "plugin", target, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(candidate / "index.json", market / "index.json")
    if run("git", "rev-parse", "HEAD", cwd=market) != plan["market_sha"]:
        raise ValueError("市场检出提交不符")
    run("git", "switch", "-c", branch, cwd=market)
    run("git", "add", f"plugins/{PLUGIN}", "index.json", cwd=market)
    changed = run("git", "diff", "--cached", "--name-only", cwd=market).splitlines()
    if not changed or any(
        name != "index.json" and not name.startswith(f"plugins/{PLUGIN}/") for name in changed
    ):
        raise ValueError("提交范围必须仅限插件目录与生成的索引")
    run(
        "git",
        "-c",
        "user.name=MovieClaw Trakt release",
        "-c",
        "user.email=10677990+o1xhack@users.noreply.github.com",
        "commit",
        "-m",
        f"Update {PLUGIN} to {version}",
        cwd=market,
    )
    # 分支只创建一次，不 force-push；分支存在或并发写入时明确失败。
    run(
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
        "push",
        fork_url,
        f"HEAD:refs/heads/{branch}",
        cwd=market,
    )
    body = (candidate / "pr-body.md").read_text()
    result = api(
        f"repos/{MARKET}/pulls",
        {
            "title": f"Update {PLUGIN} to {version}",
            "head": f"o1xhack:{branch}",
            "base": "main",
            "body": body,
        },
    )
    print(result["html_url"])


def validation(candidate):
    return {
        name: hashlib.sha256((candidate / name).read_bytes()).hexdigest()
        for name in ("plan.json", "index.json", "pr-body.md")
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--source", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.add_argument("--tag", required=True)
    prepare_parser.add_argument("--apply", action="store_true")
    submit_parser = sub.add_parser("submit")
    submit_parser.add_argument("--candidate", type=Path, required=True)
    submit_parser.add_argument("--market", type=Path, required=True)
    validate_parser = sub.add_parser("record-validation")
    validate_parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args.source.resolve(), args.output.resolve(), args.tag, apply=args.apply)
        elif args.command == "submit":
            submit(args.candidate.resolve(), args.market.resolve())
        else:
            path = args.candidate.resolve()
            (path / "validated.json").write_text(json.dumps(validation(path), indent=2) + "\n")
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"市场同步停止：{exc}\n")


if __name__ == "__main__":
    main()
