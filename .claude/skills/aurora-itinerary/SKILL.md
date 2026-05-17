---
name: aurora-itinerary
description: Generate an Aurora-branded PDF itinerary from a pasted context preview plus uploaded photos and PDF attachments. Use when the user wants to turn an Aurora itinerary draft (markdown with `[Image: ...]` / `[Attachment: ...]` references and `### C/ Date: Title` section headers) into a finished, brand-aligned PDF. Handles photo→reference matching even when uploaded filenames don't match the refs in the markdown.
---

# Aurora Itinerary PDF Generator

This skill turns an Aurora itinerary draft into a finished, brand-aligned PDF. It applies the Aurora Brand Guidelines 1.0 visual system (Fireplace Orange gradient cover, Sunrise Cream paper, Manrope typography, AURORA wordmark) and respects per-day photo placement for multi-day cruise / hotel sections.

## What the user provides

1. **The itinerary text** — pasted into chat, in the Aurora context-preview markdown format. Recognised structural cues:
   - Front matter: `Member: <name>`, `Thread: <subject>`, `Hero image: [Image: <filename>]`, then a `---` divider.
   - `# <Trip title>` — document title.
   - `## Introduction`, `## Recommendation Items`, etc. — top-level blocks.
   - `### C/ <Date>: <Title>` — each itinerary item (the `C/` prefix is the reliable section marker).
   - `[Image: <filename-or-stem>]` — photo references.
   - `[Gallery: <name>]` — start a labelled gallery; subsequent `[Image: ...]` lines belong to it until the next non-image line.
   - `[Attachment: <name>]` — clickable card that opens an embedded PDF.
   - Inside multi-day sections (cruises), `**Day N, …**` paragraphs act as day dividers; photos that follow each day sit *with that day*.

2. **The photos** — uploaded as a zip, multiple zips, or loose files. Filenames may or may not match the `[Image: ...]` references.

3. **The PDF attachments** (optional) — any `[Attachment: ...]` referenced in the markdown should have a matching PDF file uploaded. Filename matching is fuzzy (case- and punctuation-insensitive), so `Aqua_Nera_Confirmation.pdf` will satisfy `[Attachment: Aqua Nera Confirmation]`.

## Workflow

When this skill is invoked, drive it end-to-end:

### 1. Set up a working directory

Create a fresh working directory (e.g. `/tmp/aurora-<timestamp>/`) with `images/` and `attachments/` subfolders. Don't pollute the user's repo working tree.

```bash
WORK=/tmp/aurora-$(date +%s)
mkdir -p "$WORK"/{images,attachments}
```

### 2. Persist the pasted markdown

Save the user's pasted itinerary text verbatim to `$WORK/itinerary.md`. **Do not** rewrite or clean it up unless the user asks — the parser handles the format as-is.

### 3. Ingest uploaded files

- **Zip archives**: extract into `$WORK/_unpack/` (flat — strip nested directories). Skip macOS metadata noise (`__MACOSX/`, `._*`, `.DS_Store`).
- **Loose files**: copy directly into `$WORK/_unpack/`.

### 4. Match photos to `[Image: ...]` references — three strategies in order

For each strategy, only fall through to the next if it doesn't produce a clean 1:1 mapping.

**Strategy A — direct filename match.** If the uploaded filename's stem matches a `[Image: ...]` reference's stem (case-insensitive), it lands on that ref. Many real Aurora itineraries have refs like `[Image: 8ab81d1240a4.jpeg]` where the filename is already the ref — these match for free.

**Strategy B — positional via upload order.** If only some files match by name, look at the user's CSV / file listing for upload order (Date Modified column from Finder, or zip internal order). The Aurora context-preview format produces images and PDF attachments in the same sequence the user downloaded them, so position N in upload order ↔ position N in the doc's combined `[Image:]` + `[Attachment:]` sequence. **Important:** the doc sequence interleaves images and attachments — when matching, walk both in source order, with PDFs in the upload list landing on `[Attachment: ...]` slots.

**Strategy C — ask the user.** If neither name nor position resolves cleanly (e.g. mixed sources, missing files, different counts), produce a CSV draft mapping `doc_ref → uploaded_filename` and ask the user to correct it.

When matching by position (B), validate by checking that any already-matching filenames (from Strategy A) land in their expected positions — if they do, the order is correct and the remap is safe to apply to the unmatched files.

### 5. Stage files into the working directory

After matching:

- Copy each photo into `$WORK/images/` **renamed to the ref's filename** so the parser's filename-matching can find it. Preserve the source extension; if the ref includes an extension, use that.
- Copy each PDF attachment into `$WORK/attachments/` with a name that the fuzzy matcher will hit. The simplest is to name it `<Attachment label>.pdf` (e.g. `Aqua Nera Confirmation.pdf`).

### 6. Run the generator

Always pass `--compress` for the final deliverable — it shrinks the PDF ~10x with no visible loss at A4 viewing size.

```bash
python "$CLAUDE_PROJECT_DIR"/.claude/skills/aurora-itinerary/scripts/generate.py \
  --input "$WORK/itinerary.md" \
  --images "$WORK/images" \
  --attachments "$WORK/attachments" \
  --out "$WORK/itinerary.pdf" \
  --compress
```

The script logs `Images: N resolved / M missing` and `Embedded K attachment(s)`. Investigate any non-zero `missing` count before delivering — it usually means a mapping issue from step 4.

### 7. Deliver

Send the compressed PDF to the user with `SendUserFile`. Include a short caption: number of pages, photos, embedded attachments, and final file size.

## Dependencies

The script needs these Python packages (install once with pip):

```
jinja2          # template engine
weasyprint      # HTML → PDF
markdown-it-py  # markdown parsing
pikepdf         # PDF post-processing (attachments + GoToE links)
Pillow          # only needed if pre-processing photos
```

System dependencies for WeasyPrint:
- Debian/Ubuntu: `apt-get install libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libffi-dev ghostscript`
- macOS: `brew install pango cairo gdk-pixbuf libffi ghostscript`

Ghostscript is needed for `--compress`. If absent, the script logs a warning and ships the uncompressed PDF.

## What the skill controls (don't second-guess)

- **Brand styling.** Cover gradient, palette, typography, section rule, day-heading treatment — all set in `templates/styles.css` and `templates/itinerary.html.j2`. Modify those files if the user wants a brand revision; don't override in user CSS.
- **Photo cap.** Hard-capped at 3 photos per image row. This is by design — Aurora is a luxury concierge brand and the photo rule keeps pages calm. If the user wants more, edit `_render_image_grid_factory` in `scripts/generate.py`.
- **Per-day photo placement.** The parser captures content as an ordered `blocks` list so each day's photos render under its own day. Don't try to "fix" the markdown by moving images around — leave them where the source puts them.

## What the user can override per-run

- Add `--strict-images` to the script call if every image must resolve (useful for client-ready builds).
- Skip `--compress` if the user wants the full-resolution print master.
- Add `--html-only` to dump the rendered HTML for debugging.

## Edge cases

- **Photo filenames with no extension** (e.g. `[Image: phenom300_flexjet]`). The parser's `resolve_image` does extension-agnostic matching, so the file on disk just needs to share the stem. Rename uploaded photos to the *stem* of the ref if the ref has no extension.
- **Special characters in refs** (`+`, `&`, accents). The parser matches them verbatim — rename uploaded files to include them as-is.
- **Hero image.** The `Hero image: [Image: ...]` front-matter ref drives the cover photo. If the user's draft doesn't have one, the cover renders on the gradient alone.
- **More photos than days.** Within a multi-day section, each day's photo run is independently capped at 3. Extras are dropped silently — the brand guideline favours concision.
