#!/usr/bin/env python3
"""Install a committed version of pr-digest for the menu bar and the watcher to run.

The menu, the watcher, the lessons job and the prdigest:// link handler never run the
checkout's working tree: editing a file there changes nothing live. They run an
installed release, built from a commit with `git archive`, so uncommitted edits are
never deployed:

    ~/.local/share/pr-digest/
      releases/<sha>/   the code of one commit, plus `.venv` (link to ../../venv) and
                        `.data_dir` (the checkout, where state/ and output/ stay)
      current           link to the release that runs; a deploy flips it atomically
      venv/             one virtualenv, rebuilt when requirements.txt changes
      plugins/          SwiftBar's plugin folder: the launchers, pointed at current

    bin/deploy [REF]          install REF (default HEAD) and switch to it
    bin/deploy --setup [REF]  also point SwiftBar, the launchd jobs and the link
                              handler at the install (once, or after their wiring
                              changes)
    bin/deploy --status       which release runs, and whether the checkout differs

Data stays in the checkout (settings.DATA_DIR), so a deploy never moves state.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("PR_DIGEST_INSTALL", "~/.local/share/pr-digest")).expanduser()
RELEASES = ROOT / "releases"
CURRENT = ROOT / "current"
VENV = ROOT / "venv"
PLUGINS = ROOT / "plugins"
KEEP = 3
SWIFTBAR_DOMAIN = "com.ameba.SwiftBar"
# Where SwiftBar kept the menu's cache while the plugin ran from the checkout.
OLD_MENU_DATA = Path("~/Library/Application Support/SwiftBar/Plugins").expanduser() / (
    str(CHECKOUT / "swiftbar-plugins" / "prdigest.1h.sh").lstrip("/"))


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(CHECKOUT), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def build_release(sha: str) -> Path:
    release = RELEASES / sha[:12]
    if release.exists():
        return release
    tmp = RELEASES / f".{sha[:12]}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(CHECKOUT), "archive", "--format=tar", sha],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tmp, filter="tar")
    (tmp / ".venv").symlink_to(Path("..") / ".." / "venv")
    (tmp / ".data_dir").write_text(f"{CHECKOUT}\n")
    (tmp / ".release").write_text(f"{sha}\n")
    tmp.rename(release)
    return release


def ensure_venv(release: Path) -> None:
    """One virtualenv for every release, reinstalled when requirements.txt changes."""
    req = release / "requirements.txt"
    want = hashlib.sha256(req.read_bytes()).hexdigest()
    stamp = VENV / ".requirements.sha256"
    if stamp.exists() and stamp.read_text().strip() == want:
        return
    if not (VENV / "bin" / "python").exists():
        base = Path(sys.base_prefix) / "bin" / "python3"
        subprocess.run([str(base), "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(VENV / "bin" / "python"), "-m", "pip", "install", "-q",
                    "-r", str(req)], check=True)
    stamp.write_text(want + "\n")


def switch(release: Path) -> None:
    tmp = ROOT / ".current.tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(Path("releases") / release.name)
    os.replace(tmp, CURRENT)


def prune(keep: int = KEEP) -> None:
    live = CURRENT.resolve().name
    old = sorted((p for p in RELEASES.iterdir() if p.is_dir() and not p.name.startswith(".")),
                 key=lambda p: p.stat().st_mtime, reverse=True)
    for p in old[keep:]:
        if p.name != live:
            shutil.rmtree(p, ignore_errors=True)


def write_plugins(release: Path) -> None:
    """SwiftBar's plugin folder: each of the release's launchers, with its repo path
    pinned to `current` so a deploy switches them without touching SwiftBar."""
    PLUGINS.mkdir(parents=True, exist_ok=True)
    for src in sorted((release / "swiftbar-plugins").glob("*.sh")):
        lines = src.read_text().splitlines()
        out = [f'REPO="{CURRENT}"' if ln.startswith("REPO=") else ln for ln in lines]
        dest = PLUGINS / src.name
        dest.write_text("\n".join(out) + "\n")
        dest.chmod(0o755)


def migrate_menu_state() -> None:
    """Carry the menu's cache and dismissals from SwiftBar's per-plugin folder into
    state/menu, where the menu now keeps them."""
    sys.path.insert(0, str(CHECKOUT))
    import settings
    dest = settings.STATE_DIR / "menu"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("cache.json", "dismissed.json"):
        src = OLD_MENU_DATA / name
        if src.exists() and not (dest / name).exists():
            shutil.copy2(src, dest / name)
            print(f"moved the menu's {name} into {dest}")


def setup() -> int:
    migrate_menu_state()
    rc = 0
    print(f"pointing SwiftBar at {PLUGINS} (it restarts)")
    subprocess.run(["osascript", "-e", 'quit app "SwiftBar"'], capture_output=True)
    subprocess.run(["defaults", "write", SWIFTBAR_DOMAIN, "PluginDirectory", str(PLUGINS)],
                   check=True)
    subprocess.run(["open", "-a", "SwiftBar"], check=True)
    print("installing the launchd jobs from the install")
    rc |= subprocess.run([str(CURRENT / ".venv" / "bin" / "python"), "-m", "watcher", "install"],
                         cwd=CURRENT).returncode
    print("re-registering the prdigest:// link handler")
    rc |= subprocess.run([str(CURRENT / "url-handler" / "install.sh")]).returncode
    return rc


def status() -> int:
    if not CURRENT.exists():
        print("not installed: run bin/deploy --setup")
        return 1
    running = (CURRENT / ".release").read_text().strip()
    head = git("rev-parse", "HEAD")
    print(f"running {running[:12]} ({git('log', '-1', '--format=%s', running)})")
    if running != head:
        ahead = git("rev-list", "--count", f"{running}..{head}")
        print(f"checkout HEAD is {head[:12]}, {ahead} commit(s) newer: bin/deploy to switch")
    if git("status", "--porcelain", "--untracked-files=no"):
        print("the checkout has uncommitted changes; they are not deployed")
    plugin_dir = subprocess.run(["defaults", "read", SWIFTBAR_DOMAIN, "PluginDirectory"],
                                capture_output=True, text=True).stdout.strip()
    print(f"SwiftBar plugin folder: {plugin_dir}"
          + ("" if plugin_dir == str(PLUGINS) else "  (not the install: run --setup)"))
    return 0


def main(argv: list[str]) -> int:
    if "--status" in argv:
        return status()
    refs = [a for a in argv if not a.startswith("--")]
    sha = git("rev-parse", "--verify", f"{refs[0] if refs else 'HEAD'}^{{commit}}")
    if git("status", "--porcelain", "--untracked-files=no"):
        print("note: the checkout has uncommitted changes; only the commit is deployed")
    release = build_release(sha)
    ensure_venv(release)
    write_plugins(release)
    switch(release)
    prune()
    print(f"deployed {sha[:12]} ({git('log', '-1', '--format=%s', sha)}) to {CURRENT}")
    rc = setup() if "--setup" in argv else 0
    subprocess.run(["open", "-g", "swiftbar://refreshplugin?name=prdigest.1h.sh"],
                   capture_output=True)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
