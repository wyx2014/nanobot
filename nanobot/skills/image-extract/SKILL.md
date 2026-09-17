---
name: image-extract
description: 通过内置多模态服务识别图片中的全部文字或提取指定字段。用户要求识别图片内容，或从图片、发票、表格、单据中提取账号、证件号、金额等信息时使用。
metadata: {"nanobot":{"requires":{"env":["NANOBOT_IMAGE_EXTRACT_API_URL","NANOBOT_IMAGE_EXTRACT_API_KEY","NANOBOT_IMAGE_EXTRACT_MODEL"]}}}
---

# Image Extract

Use `extract_image` for local PNG, JPEG, GIF, WebP, and BMP images.

- Pass the exact local attachment or workspace path as `image_path`.
- For a general recognition request, omit `prompt` or use `提取图中的全部内容`.
- For requested fields, make `prompt` specific, for example `提取发票上的金额和税号`.
- Return the extracted result directly. Preserve line breaks, table structure, labels, numbers,
  punctuation, and leading zeros when present.
- Do not guess unreadable text. Mark uncertain characters or fields clearly.
- If the tool reports an unavailable service, unsupported format, missing file, or boundary error,
  report that concrete error once and do not retry through shell commands.

The bundled `scripts/extract_image.py` is for developer diagnostics. Normal agent execution must use
the native `extract_image` tool so installed desktop users do not need a separate Python runtime.
