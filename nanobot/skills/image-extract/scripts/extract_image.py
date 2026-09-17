#!/usr/bin/env python3
"""Developer CLI for the managed image extraction service."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nanobot.agent.tools.image_extract import ImageExtractionClient, ImageExtractionError


def main() -> int:
    parser = argparse.ArgumentParser(description="从图片中提取内容（多模态接口）")
    parser.add_argument("image", help="图片文件路径")
    parser.add_argument(
        "--prompt",
        default="提取图中的全部内容",
        help="提取提示词，例如：提取图中的银行账号",
    )
    parser.add_argument("--max-tokens", type=int, default=4096)
    args = parser.parse_args()

    image = Path(args.image).expanduser()
    if not image.is_file():
        print(f"ERROR: 图片不存在: {image}", file=sys.stderr)
        return 1
    if not 1 <= args.max_tokens <= 16384:
        print("ERROR: --max-tokens 必须在 1 到 16384 之间", file=sys.stderr)
        return 1

    try:
        text = ImageExtractionClient.from_environment().extract(
            image,
            args.prompt,
            args.max_tokens,
        )
    except ImageExtractionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
