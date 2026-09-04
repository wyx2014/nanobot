---
name: corporate-ppt
description: 使用内置中国太平公司模板规划视觉素材并创建图文丰富、可编辑、可校验的 PPTX。当用户要求生成 PPT、幻灯片、汇报材料，或明确要求套用公司模板时使用。
---

# Corporate PPT

Create company-branded presentations with a visual-first workflow. The nanobot runtime owns asset
import, template-aware rendering and validation. Do not write OOXML manually, rasterize complete
slides, or send internal documents to public presentation services.

## Required Workflow

1. Read the user's source material and identify audience, decision or learning task, confidentiality,
   page count and evidence boundaries.
2. Read [references/deck-format.md](references/deck-format.md). For decks longer than five pages,
   requests for visual polish, or topics involving products, people, places, events, interfaces or
   physical environments, also read
   [references/visual-guidelines.md](references/visual-guidelines.md).
3. Write a page outline before the deck specification. Give every content page one conclusion and
   one main exhibit: image, chart, table, timeline, process, comparison, metrics or concise text.
   Do not default most pages to `title-body`.
4. Create a self-contained project:

   ```text
   presentation-name/
     deck.yaml
     visual-plan.yaml     # for visual-first decks
     media/
     presentation-name.pptx
     presentation-name-preview.pdf  # when a local renderer is available
   ```

5. For visual-first decks, prepare the media before finalizing `deck.yaml`:
   - Prefer user-provided and approved internal images.
   - For a public topic, use official or credible public sources only when web access and project
     policy permit it. Locate the owning page first, then pass that page as `source_page_url` to
     `import_presentation_asset`; if a verified direct image URL is already available, pass both
     `source_url` and `source_page_url`. Record the owning page in the slide `source`.
   - Never use `web_fetch` to download an image binary, invent an upload path, or derive a direct
     image URL from a filename. For Wikimedia Commons, use a real `File:` result page rather than
     constructing an `upload.wikimedia.org` URL.
   - For each needed subject, try at most three distinct credible owning pages, moving from official
     sources to archives or Commons. Do not retry the same failing URL. One unavailable image must
     not cancel acquisition for the remaining pages.
   - When `generate_image` is available and policy permits generation, create only conceptual or
     atmospheric visuals, then use `import_presentation_asset` with its artifact path.
   - Only after the bounded source attempts fail, mark that specific asset as an evidence gap and
     use an editable `timeline`, `process`, `table`, `chart`, `comparison` or `metrics` exhibit for
     that page. Never fabricate numeric values to make a chart.
6. Write a version 1 CorporateDeck YAML or JSON file. Keep every image below the project directory
   and reference it with a relative path.
7. Call `create_presentation` with explicit source and output paths. Request a PDF preview for decks
   that need visual review. Preview failure must not discard a valid PPTX.
8. Review validation warnings. Fix repeated media, long text, low visual-layout use, repetitive page
   rhythm, missing sources or density problems, then regenerate.
9. When a preview exists and image inspection is available, inspect every page for cropping,
   distortion, overlap, weak contrast, inconsistent spacing and branding obstruction. Otherwise
   perform structural review and disclose that rendered visual QA was unavailable.
10. Deliver the editable PPTX, `deck.yaml`, visual plan and preview when present.

## Invariants

- Preserve the company cover, master branding, closing page, logo, colors and 16:9 dimensions.
- Keep charts, tables, text, timelines and processes editable in PowerPoint or WPS.
- Use images as evidence or explanation, not decoration. Do not count logos, tiny icons or repeated
  chart screenshots as substantive imagery.
- Do not place confidential names, data or documents into web search or image-generation prompts.
- Public-image use must retain the source URL or source-page label. Internal images use an approved
  document or asset-library label.
- Avoid three consecutive pages with the same body layout. A rich deck varies rhythm without mixing
  unrelated visual styles.
- Do not overwrite existing outputs unless the user requested replacement.

Read [references/content-guidelines.md](references/content-guidelines.md) for density and evidence
rules. The machine-readable schema is
[references/deck-schema.json](references/deck-schema.json).
