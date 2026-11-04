"""Read a review draft or follow-up report as a web page, and discuss it with Claude.

    .venv/bin/python review/view_draft.py <draft.md>         render + open in the browser
    .venv/bin/python review/view_draft.py --handle-url <url> what the prdigest:// handler runs

The draft is rendered locally (Python-Markdown: tables, fenced code, `<details>`
sections, bare links made clickable) into output/drafts-html/<name>.html and opened in
the default browser. Nothing leaves the machine.

The page's "Discuss with Claude" button is a `prdigest://discuss?file=<path>` link.
url-handler/install.sh registers a tiny app for that scheme, and the app runs this
script with --handle-url. Because any web page can open a prdigest:// link, the
handler trusts nothing in the URL except the file path: the path must resolve to an
existing .md file inside one of the draft folders (review.drafts_dir,
watcher.reports_dir), and the prompt the Claude session starts with is built here, not
taken from the URL.
"""

from __future__ import annotations

import html
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import markdown  # noqa: E402

import session  # noqa: E402
import settings  # noqa: E402

REPO_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_DIR / "output" / "drafts-html"
SCHEME = "prdigest"

_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
# A bare URL not already inside a markdown link, autolink, or HTML attribute.
_BARE_URL = re.compile(r'(?<![(<"\'=])\bhttps?://[^\s<>)\]]+')


def allowed_dirs(cfg: dict | None = None) -> list[Path]:
    cfg = cfg or settings.load()
    return [settings.path_of(cfg["review"]["drafts_dir"]).resolve(),
            settings.path_of(cfg["watcher"]["reports_dir"]).resolve()]


def kind_of(path: Path, cfg: dict | None = None) -> str:
    """"review" for a code-review draft, "followup" for a follow-up report."""
    reports = settings.path_of((cfg or settings.load())["watcher"]["reports_dir"]).resolve()
    return "followup" if reports in path.resolve().parents else "review"


def discuss_url(path: Path) -> str:
    return f"{SCHEME}://discuss?file={quote(str(path.resolve()), safe='')}"


def _prepare(md: str) -> str:
    # Let markdown inside <details> render (md_in_html reads markdown="1").
    md = re.sub(r"<details(?![^>]*markdown=)", '<details markdown="1"', md)
    # Make bare links clickable, outside fenced and inline code.
    out, in_fence = [], False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            parts = re.split(r"(`[^`]*`)", line)
            line = "".join(p if p.startswith("`") else _BARE_URL.sub(r"<\g<0>>", p)
                           for p in parts)
        out.append(line)
    return "\n".join(out)


CSS = """
:root { --bg:#ffffff; --fg:#1f2328; --muted:#59636e; --border:#d1d9e0; --code:#f6f8fa;
        --accent:#0969da; --bar:#f6f8fa; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0d1117; --fg:#e6edf3; --muted:#9198a1; --border:#3d444d; --code:#151b23;
          --accent:#4493f8; --bar:#151b23; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
.bar { position:sticky; top:0; display:flex; gap:12px; align-items:center; flex-wrap:wrap;
       padding:10px 24px; background:var(--bar); border-bottom:1px solid var(--border); z-index:1; }
.bar .name { font-weight:600; margin-right:auto; overflow-wrap:anywhere; }
.btn { display:inline-block; padding:6px 14px; border-radius:6px; text-decoration:none;
       border:1px solid var(--border); color:var(--fg); background:var(--bg); font-size:14px; }
.btn.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
main { max-width:1000px; margin:0 auto; padding:24px; }
a { color:var(--accent); }
h1,h2,h3 { line-height:1.3; margin-top:1.6em; }
h2 { border-bottom:1px solid var(--border); padding-bottom:.3em; }
code { background:var(--code); padding:.15em .35em; border-radius:4px; font-size:85%;
       font-family:ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap:anywhere; }
pre { background:var(--code); padding:14px; border-radius:6px; overflow:auto; }
pre code { padding:0; background:none; font-size:85%; }
table { border-collapse:collapse; display:block; overflow:auto; margin:1em 0; }
th,td { border:1px solid var(--border); padding:6px 12px; vertical-align:top; }
th { background:var(--code); }
details { border:1px solid var(--border); border-radius:6px; padding:8px 14px; margin:1em 0; }
summary { cursor:pointer; font-weight:600; }
blockquote { margin:0; padding:0 1em; color:var(--muted); border-left:4px solid var(--border); }
.hint { color:var(--muted); font-size:13px; }
"""


def render(path: Path) -> Path:
    """Write the HTML page for one draft and return its path."""
    text = path.read_text(errors="replace")
    body = markdown.markdown(_prepare(text), extensions=[
        "tables", "fenced_code", "md_in_html", "sane_lists"])
    pr = _PR_URL.search(text)
    pr_link = (f'<a class="btn" href="{html.escape(pr.group(0))}">Open PR on GitHub</a>'
               if pr else "")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(path.stem)}</title><style>{CSS}</style></head>
<body>
<div class="bar">
  <span class="name">{html.escape(path.name)}</span>
  {pr_link}
  <a class="btn primary" href="{html.escape(discuss_url(path))}"
     title="Opens a Claude Code session in iTerm, primed on this draft">💬 Discuss with Claude</a>
  <a class="btn" href="file://{html.escape(quote(str(path.resolve())))}">Raw markdown</a>
</div>
<main>{body}
<p class="hint">Rendered from {html.escape(str(path))}. Re-open from the menu to refresh.</p>
</main></body></html>
"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{path.stem}.html"
    out.write_text(page, encoding="utf-8")
    return out


def validated_path(url: str, cfg: dict | None = None) -> Path:
    """The draft a prdigest://discuss URL points at, or ValueError when the URL isn't
    a discuss link to an existing .md file inside an allowed draft folder."""
    parsed = urlparse(url)
    if parsed.scheme != SCHEME or parsed.netloc != "discuss":
        raise ValueError(f"not a {SCHEME}://discuss link")
    files = parse_qs(parsed.query).get("file") or []
    if len(files) != 1:
        raise ValueError("expected exactly one file=")
    path = Path(files[0]).resolve()
    if path.suffix != ".md" or not path.is_file():
        raise ValueError(f"not an existing .md file: {path}")
    if not any(d in path.parents for d in allowed_dirs(cfg)):
        raise ValueError(f"outside the draft folders: {path}")
    return path


def prompt_for(path: Path, cfg: dict | None = None) -> str:
    if kind_of(path, cfg) == "followup":
        prompt = f"Let's go over the follow-up on my review comment: {path}"
    else:
        prompt = f"Let's go over this review draft: {path}"
    pr = _PR_URL.search(path.read_text(errors="replace"))
    if pr:
        import status_report
        prompt += f" The PR's current status is in {status_report.path_for(pr.group(0))}."
    return prompt


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--handle-url"] and len(argv) == 2:
        try:
            path = validated_path(argv[1])
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        return session.launch(prompt_for(path))
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    page = render(Path(argv[0]).expanduser())
    return subprocess.run(["/usr/bin/open", str(page)]).returncode


if __name__ == "__main__":
    sys.exit(main())
