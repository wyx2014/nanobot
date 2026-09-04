#!/usr/bin/env python3
"""Generate an editable PPTX from a CorporateDeck specification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ppt_common import (
    DEFAULT_TEMPLATE,
    DeckSpecError,
    generate_presentation,
    load_deck_spec,
    spec_warnings,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a branded editable PPTX from a CorporateDeck YAML or JSON file."
    )
    parser.add_argument("spec", type=Path, help="CorporateDeck .yaml, .yml or .json file")
    parser.add_argument("--output", "-o", type=Path, required=True, help="output .pptx file")
    parser.add_argument(
        "--template",
        type=Path,
        default=DEFAULT_TEMPLATE,
        help="PowerPoint template (defaults to the bundled corporate template)",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    return parser


def generate(spec_path: Path, output: Path, template: Path, *, force: bool = False) -> dict:
    spec_source = spec_path.expanduser().resolve()
    output_path = output.expanduser().resolve()
    if output_path.suffix.lower() != ".pptx":
        raise DeckSpecError("output path must end in .pptx")
    if output_path.exists() and not force:
        raise DeckSpecError(f"output already exists; pass --force to replace it: {output_path}")

    spec = load_deck_spec(spec_source)
    presentation = generate_presentation(
        spec,
        spec_path=spec_source,
        template_path=template,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    presentation.save(output_path)
    return {
        "ok": True,
        "output": str(output_path),
        "slides": len(spec["slides"]),
        "template": str(template.expanduser().resolve()),
        "warnings": spec_warnings(spec),
    }


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = generate(args.spec, args.output, args.template, force=args.force)
    except (DeckSpecError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
