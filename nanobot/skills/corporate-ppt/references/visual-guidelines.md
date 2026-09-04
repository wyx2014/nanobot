# Visual-First Presentation Workflow

Use this guide when visual quality matters or when a deck has more than five pages.

## 1. Build The Page Plan

Before downloading or generating assets, create `visual-plan.yaml`:

```yaml
scenario: technology-history
audience: internal employees
confidentiality: public-topic
pages:
  - page: 3
    conclusion: Early agents were embodied systems, not only chat software
    exhibit: image-grid
    assets:
      - subject: Shakey robot archival photograph
        source_kind: official-public
        source_page: https://example.org/archive
        path: media/shakey.jpg
  - page: 4
    conclusion: Agent capabilities evolved through four technical eras
    exhibit: timeline
    assets: []
```

For each content page record its conclusion, main exhibit, needed assets and source. Design around
the real proportions of acquired images instead of inventing image boxes first.

## 2. Choose Visual Evidence

Use this priority order:

1. User-provided images and approved internal asset libraries.
2. Official public websites, reports, archives, press kits and product documentation.
3. Credible public sources whose owning page and attribution are available.
4. Generated conceptual imagery when `generate_image` is available and policy permits it.
5. Editable diagrams, timelines, tables or charts based on verified content.

Photos and screenshots should show a concrete person, product, place, event, interface or physical
environment relevant to the slide conclusion. A logo, repeated chart screenshot, decorative texture
or tiny thumbnail is not substantive visual evidence.

Do not search confidential project names, customer information, financial figures or internal
document text. For confidential decks, use attached media and editable diagrams only unless the user
explicitly approves another source.

## 3. Acquire Assets In A Batch

1. Use `web_search` to locate the official or credible owning page.
2. Verify that the image depicts the intended subject and note the page URL.
3. Use `import_presentation_asset` with `source_page_url` and a project-local `media/...` output.
   The importer resolves standard Open Graph or Twitter image metadata. If the page exposes a
   verified direct image URL, pass it as `source_url` while retaining `source_page_url` for the
   HTTP Referer and attribution.
4. For generated images, call `generate_image` only when it is registered, then pass the returned
   artifact path to `import_presentation_asset` as `source_path`.
5. Add the source-page URL or internal asset label to the slide `source` field.

Do not use `web_fetch` for image binaries, arbitrary image-search thumbnails as final assets, or
guessed direct URLs. A 403, 404, TLS error, non-image response or unsupported format means the
candidate failed; it does not prove that the topic has no usable image. Try at most three distinct
owning pages for that subject, never retry the same URL, then record a page-specific evidence gap and
use a native editable exhibit. Continue acquiring the other planned images. Do not reuse one image on
more than two slides unless it is an intentional before/after or persistent reference object.

## 4. Select The Main Exhibit

| Communication need | Preferred slide type |
| --- | --- |
| One concrete subject plus explanation | `image-text` |
| Two to six related photos or screenshots | `image-grid` |
| Chronological evolution | `timeline` |
| Ordered work or operating stages | `process` |
| Verified numeric relationship | `chart` |
| Exact values or multi-dimensional comparison | `table` |
| A few headline numbers | `metrics` |
| Two alternatives or states | `comparison` |
| One detailed architecture or screenshot | `full-image` |

Use `title-body` only when prose is genuinely the clearest exhibit. Avoid three consecutive pages
with the same layout. Do not force images onto pages where a relationship diagram is clearer.

## 5. Data And Attribution

Every native chart requires `source`. Include the reporting period, unit and comparison basis. If
values are illustrative, label them explicitly; otherwise never estimate values for visual effect.
External images retain the owning page URL, not merely a search-result URL. Internal assets cite the
approved document, system, team or library label without exposing confidential data.

## 6. Visual Review

When a PDF preview exists, inspect every page at normal reading size and zoom suspicious pages.
Check:

- images are sharp, proportionally scaled and focused on the intended subject;
- text does not cover faces, product details, logos or evidence;
- titles, exhibits, explanations and sources form one clear reading order;
- slide types vary while title anchors, typography and brand semantics stay consistent;
- tables and charts remain readable and editable;
- no content enters the company logo, frame, footer or page-number areas.

When rendered preview is unavailable, inspect the deck specification, media dimensions, slide-type
rhythm, text density, source coverage and validation warnings. State that full rendered QA was not
performed.
