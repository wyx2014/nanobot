#!/usr/bin/env python3
"""Render a PPTX to PDF with local LibreOffice or macOS Quick Look."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ppt_common import DeckSpecError


def _find_soffice() -> str:
    configured = os.environ.get("LIBREOFFICE_BIN", "").strip()
    candidate = configured or shutil.which("soffice") or shutil.which("libreoffice")
    if not candidate:
        raise DeckSpecError(
            "LibreOffice is not installed; PPTX generation still works, but PDF preview is unavailable"
        )
    try:
        probe = subprocess.run(
            [candidate, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeckSpecError(f"LibreOffice executable is not usable: {candidate}: {exc}") from exc
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip()
        raise DeckSpecError(f"LibreOffice executable is not usable: {candidate}: {detail}")
    return candidate


def render(deck_path: Path, output_path: Path, *, force: bool = False) -> dict:
    deck = deck_path.expanduser().resolve()
    output = output_path.expanduser().resolve()
    if not deck.is_file() or deck.suffix.lower() != ".pptx":
        raise DeckSpecError(f"PowerPoint input not found or invalid: {deck}")
    if output.suffix.lower() != ".pdf":
        raise DeckSpecError("preview output must end in .pdf")
    if output.exists() and not force:
        raise DeckSpecError(f"output already exists; pass --force to replace it: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    try:
        _render_with_libreoffice(deck, output)
        renderer = "libreoffice"
    except DeckSpecError as exc:
        errors.append(str(exc))
        if sys.platform != "darwin":
            raise
        try:
            _render_with_quicklook(deck, output)
            renderer = "macos-quicklook"
        except DeckSpecError as fallback_exc:
            errors.append(str(fallback_exc))
            raise DeckSpecError("; ".join(errors)) from fallback_exc
    return {"ok": True, "deck": str(deck), "preview": str(output), "renderer": renderer}


def _render_with_libreoffice(deck: Path, output: Path) -> None:
    soffice = _find_soffice()
    with tempfile.TemporaryDirectory(prefix="corporate-ppt-") as temporary:
        completed = subprocess.run(
            [
                soffice,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                temporary,
                str(deck),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        generated = Path(temporary) / f"{deck.stem}.pdf"
        if completed.returncode != 0 or not generated.is_file():
            detail = (completed.stderr or completed.stdout).strip()
            raise DeckSpecError(f"LibreOffice PDF conversion failed: {detail or 'no PDF produced'}")
        if output.exists():
            output.unlink()
        shutil.move(str(generated), output)


def _render_with_quicklook(deck: Path, output: Path) -> None:
    qlmanage = shutil.which("qlmanage")
    sips = shutil.which("sips")
    chromium = _find_chromium()
    if not qlmanage or not sips or not chromium:
        raise DeckSpecError(
            "macOS Quick Look preview requires qlmanage, sips and Chrome/Chromium/Edge"
        )
    with tempfile.TemporaryDirectory(prefix="corporate-ppt-quicklook-") as temporary:
        root = Path(temporary)
        completed = subprocess.run(
            [qlmanage, "-p", "-o", str(root), str(deck)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        previews = list(root.glob("*.qlpreview/Preview.html"))
        if completed.returncode != 0 or len(previews) != 1:
            detail = (completed.stderr or completed.stdout).strip()
            raise DeckSpecError(f"Quick Look preview failed: {detail or 'no preview produced'}")
        preview = previews[0]
        for pdf in preview.parent.glob("Attachment*.pdf"):
            png = pdf.with_suffix(".png")
            converted = subprocess.run(
                [sips, "-s", "format", "png", str(pdf), "--out", str(png)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if converted.returncode != 0 or not png.is_file():
                raise DeckSpecError(f"Quick Look attachment conversion failed: {pdf.name}")
        html = preview.read_text(encoding="utf-8")
        html = re.sub(r"(Attachment\d+)\.pdf", r"\1.png", html)
        print_css = (
            "<style>@page{size:10in 5.625in;margin:0}"
            "@media print{html,body{margin:0!important;background:white!important;}"
            "div.slide{margin:0!important;box-shadow:none!important;page-break-after:always;}"
            "div.slide:last-of-type{page-break-after:auto;}}</style>"
        )
        html = html.replace("</head>", print_css + "</head>", 1)
        preview.write_text(html, encoding="utf-8")
        generated = root / "preview.pdf"
        printed = subprocess.run(
            [
                chromium,
                "--headless=new",
                "--disable-gpu",
                "--allow-file-access-from-files",
                "--no-pdf-header-footer",
                f"--print-to-pdf={generated}",
                preview.as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if printed.returncode != 0 or not generated.is_file():
            detail = (printed.stderr or printed.stdout).strip()
            raise DeckSpecError(f"Chromium PDF preview failed: {detail or 'no PDF produced'}")
        if output.exists():
            output.unlink()
        shutil.move(str(generated), output)


def _find_chromium() -> str | None:
    candidates = [
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    return next((str(path) for path in candidates if path and Path(path).is_file()), None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a PowerPoint deck to a PDF preview.")
    parser.add_argument("deck", type=Path)
    parser.add_argument("--output", "-o", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = render(args.deck, args.output, force=args.force)
    except (DeckSpecError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
