"""Build a reproducible, allowlisted MovieClaw plugin archive without local settings."""

import hashlib
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(output):
    version = tomllib.loads((ROOT / "movieclaw-plugin.toml").read_text())["plugin"]["version"]
    files = [
        ROOT / name
        for name in (
            "movieclaw-plugin.toml",
            "README.md",
            "README.en.md",
            "docs/technical-notes.zh-CN.md",
            "docs/technical-notes.en.md",
            "docs/sync-workflow.zh-CN.md",
            "LICENSE",
            "PRIVACY.md",
            "APPLICATION.md",
            "SOURCE.md",
            "CHANGELOG.md",
        )
    ]
    files += sorted((ROOT / "trakt_sync").glob("*.py"))
    files += sorted(p for p in (ROOT / "web").iterdir() if p.is_file())
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"trakt-sync-{version}.mcplugin"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            if path.is_symlink():
                raise ValueError(f"Symlink not allowed: {path.name}")
            info = zipfile.ZipInfo(path.relative_to(ROOT).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(f"{digest}  {target.name}\n")
    print(f"{target.name}: {digest}")
    return target


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist")
