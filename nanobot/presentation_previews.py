"""Render local template samples with the user's existing Chromium browser."""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="presentation-preview")
_JOBS = {}


class _Styles(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.active = tag == "style" or self.active

    def handle_endtag(self, tag):
        if tag == "style":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def chromium_path() -> str | None:
    candidates = [
        os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH"),
        shutil.which("chromium"), shutil.which("google-chrome"), shutil.which("msedge"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        str(Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"),
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def guizang_preview(source: Path, design: str, cache: Path, *, render: bool = False) -> Path | None:
    template = (source / "assets" / design).read_text(encoding="utf-8")
    key = hashlib.sha256((template + "sample-v3").encode()).hexdigest()[:20]
    output = cache / f"{key}.png"
    if output.is_file():
        return output
    browser = chromium_path()
    if not browser:
        return None
    if not render:
        if output not in _JOBS:
            _JOBS[output] = _EXECUTOR.submit(guizang_preview, source, design, cache, render=True)
        return None
    styles = _Styles()
    styles.feed(template)
    if design == "template-swiss.html":
        body = '''<section class="slide accent" data-layout="SWISS-COVER-ASCII">
<div class="canvas-card"><div class="chrome-min"><div>年度战略观察</div><div>2026 / 01</div></div>
<div style="flex:1;display:flex;flex-direction:column;justify-content:center;gap:30px">
<div class="t-meta">STRATEGY / RESEARCH</div>
<h1 style="font-size:112px;font-weight:200;line-height:1.15">看见变化<br>把握增长</h1>
<p style="font-size:24px;font-weight:400">从行业趋势，到下一步行动。</p></div>
<div style="border-top:1px solid currentColor;padding-top:20px;font-size:16px">市场洞察 · 年度报告</div>
</div></section>'''
    else:
        body = '''<section class="slide hero light"><div class="chrome">
<div>年度观察 · 2026</div><div>FIELD NOTES / 01</div></div>
<div class="frame" style="display:flex;flex-direction:column;justify-content:center;gap:30px;min-height:76vh">
<div class="kicker">THE NEXT CHAPTER</div>
<h1 class="h-hero" style="font-size:98px;line-height:1.2">增长的<br><em>另一种可能</em></h1>
<p class="lead" style="font-size:25px">在变化中，找到值得长期投入的方向。</p></div>
<div class="foot"><div>研究与洞察</div><div>2026</div></div></section>'''
    html = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>'
            + "".join(styles.parts) + '\n*{letter-spacing:0!important}body{margin:0}'
            + ('.slide{padding:0!important}' if design == 'template-swiss.html' else '')
            + '</style><body>' + body + '</body></html>')
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ppt-preview-") as temporary:
        folder = Path(temporary)
        page = folder / "sample.html"
        page.write_text(html, encoding="utf-8")
        screenshot = folder / "sample.png"
        try:
            process = subprocess.Popen([
                browser, "--headless", "--disable-gpu", "--no-first-run",
                "--disable-background-networking", "--hide-scrollbars", "--no-sandbox",
                "--host-resolver-rules=MAP * ~NOTFOUND", "--window-size=1280,720",
                f"--user-data-dir={folder / 'profile'}", f"--screenshot={screenshot}",
                page.as_uri(),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name != "nt")
            try:
                deadline = time.monotonic() + 12
                while not screenshot.is_file() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.1)
            finally:
                if process.poll() is None:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            if screenshot.is_file():
                shutil.copy2(screenshot, output)
                return output
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def previews_pending(cache: Path) -> bool:
    return any(path.parent == cache and not job.done() for path, job in _JOBS.items())
