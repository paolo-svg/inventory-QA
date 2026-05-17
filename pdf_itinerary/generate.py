"""CLI: render an itinerary markdown file into a multimedia PDF.

Pipeline:
  markdown -> parser -> Jinja2 template -> WeasyPrint -> base PDF
                                                       -> pikepdf post-process
                                                          (embed sub-PDFs as
                                                           file attachments,
                                                           rewrite attachment-card
                                                           links to GoToE actions)

System dependencies for WeasyPrint:
  Debian/Ubuntu:  apt-get install libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libffi-dev
  macOS:          brew install pango cairo gdk-pixbuf libffi

Usage:
  python -m pdf_itinerary.generate \\
      --input itineraries/errol_amazon_galapagos.md \\
      --images ./images \\
      --attachments ./attachments \\
      --out errol_itinerary.pdf
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import unicodedata
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from . import parser as itinerary_parser

log = logging.getLogger("pdf_itinerary")

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
ASSETS_DIR = PACKAGE_DIR / "assets"
PLACEHOLDER = ASSETS_DIR / "placeholder.png"


def _file_uri(p: Path) -> str:
    return p.resolve().as_uri()


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", text)


def attachment_anchor(name: str) -> str:
    return "att-" + _slug(name)


def _build_image_resolver(images_dir: Path, strict: bool):
    hits: list[str] = []
    misses: list[str] = []

    def resolve(name: str) -> Optional[str]:
        path = itinerary_parser.resolve_image(name, images_dir)
        if path:
            hits.append(name)
            return _file_uri(path)
        misses.append(name)
        if strict:
            return None
        if PLACEHOLDER.exists():
            return _file_uri(PLACEHOLDER)
        return None

    return resolve, hits, misses


def _render_image_grid_factory(resolve):
    def render(names: list[str]) -> Markup:
        urls = [u for u in (resolve(n) for n in names) if u]
        if not urls:
            return Markup("")
        count = len(urls)
        klass = "one" if count == 1 else "two" if count == 2 else "three" if count == 3 else "many"
        parts = [f'<div class="img-grid {klass}">']
        for u in urls:
            parts.append(f'<img src="{u}" />')
        parts.append("</div>")
        return Markup("".join(parts))

    return render


def _extract_dates(itinerary) -> str:
    """Pull a 'May 12 - May 31, 2026' style line from the intro, if present."""
    if not itinerary.intro_html:
        return ""
    text = re.sub(r"<[^>]+>", "\n", itinerary.intro_html)
    for line in text.splitlines():
        line = line.strip()
        if re.search(r"\b\d{4}\b", line) and "-" in line:
            return line
    return ""


def _filter_sections(itinerary, only: Optional[str]):
    """Optionally narrow the itinerary down to one section by case-insensitive
    title substring match. Returns a new Itinerary view; the original is
    not mutated."""
    if not only:
        return itinerary
    needle = only.lower()
    kept = [s for s in itinerary.sections if needle in s.title.lower()]
    if not kept:
        log.error("--section %r matched no section titles.", only)
        sys.exit(2)
    log.info("Filtering to %d matching section(s): %s", len(kept), [s.title for s in kept])
    itinerary.sections = kept
    return itinerary


def render_html(itinerary, images_dir: Path, strict: bool) -> tuple[str, list[str], list[str]]:
    resolve, hits, misses = _build_image_resolver(images_dir, strict)
    render_image_grid = _render_image_grid_factory(resolve)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("itinerary.html.j2")
    hero_src = resolve(itinerary.hero_image) if itinerary.hero_image else None

    html = template.render(
        itinerary=itinerary,
        hero_src=hero_src,
        dates=_extract_dates(itinerary),
        styles_href=_file_uri(TEMPLATES_DIR / "styles.css"),
        render_image_grid=render_image_grid,
        resolve_one=resolve,
        attachment_anchor=attachment_anchor,
    )
    return html, hits, misses


def render_pdf(html: str, out_path: Path) -> None:
    from weasyprint import HTML
    HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(str(out_path))


# ---------------------------------------------------------------- Attachments

def _find_attachment_file(name: str, directory: Path) -> Optional[Path]:
    """Resolve [Attachment: X] to a real PDF in `directory`, with fuzzy match."""
    if not directory.exists():
        return None
    target_slug = _slug(name)
    candidates = [p for p in directory.iterdir() if p.is_file()]
    # Exact stem (case-insensitive)
    for p in candidates:
        if p.stem.lower() == name.lower():
            return p
    # Slug match — most forgiving (handles spaces, punctuation, capitalisation)
    for p in candidates:
        if _slug(p.stem) == target_slug:
            return p
    # Substring on the slug
    for p in candidates:
        if target_slug in _slug(p.stem) or _slug(p.stem) in target_slug:
            return p
    return None


def embed_attachments(
    pdf_path: Path,
    itinerary,
    attachments_dir: Path,
) -> tuple[list[tuple[str, Path]], list[str]]:
    """Embed referenced PDFs and rewrite attachment-card link annotations into
    GoToE (embedded go-to) actions so clicking a card opens the attachment.

    Returns (embedded, missing) — embedded is a list of (anchor, path) pairs.
    """
    import pikepdf
    from pikepdf import Name, Dictionary, Array, String

    refs: list[tuple[str, str]] = []  # (attachment label, anchor id)
    for section in itinerary.sections:
        for att in section.attachments:
            refs.append((att, attachment_anchor(att)))
    if not refs:
        return [], []

    embedded: list[tuple[str, Path]] = []
    missing: list[str] = []

    pdf = pikepdf.open(str(pdf_path), allow_overwriting_input=True)

    # Embed each referenced PDF at document level.
    anchor_to_filename: dict[str, str] = {}
    for label, anchor in refs:
        path = _find_attachment_file(label, attachments_dir)
        if not path:
            missing.append(label)
            continue
        embed_name = path.name
        pdf.attachments[embed_name] = pikepdf.AttachedFileSpec.from_filepath(pdf, path)
        anchor_to_filename[anchor] = embed_name
        embedded.append((anchor, path))

    # Walk the page link annotations. WeasyPrint renders `<a href="#anchor">`
    # as internal-link annotations whose dest is the anchor name (a Named
    # dest). Replace them with GoToE actions for the anchors that point to
    # embedded files.
    names_tree = pdf.Root.get(Name.Names)
    dest_lookup: dict[str, object] = {}
    if names_tree is not None:
        dests = names_tree.get(Name.Dests)
        if dests is not None:
            # Walk the name tree leaves.
            stack = [dests]
            while stack:
                node = stack.pop()
                if Name.Names in node:
                    arr = node[Name.Names]
                    for i in range(0, len(arr), 2):
                        key = str(arr[i])
                        dest_lookup[key] = arr[i + 1]
                if Name.Kids in node:
                    for kid in node[Name.Kids]:
                        stack.append(kid)

    rewritten = 0
    for page in pdf.pages:
        annots = page.get(Name.Annots)
        if annots is None:
            continue
        for annot in annots:
            if annot.get(Name.Subtype) != Name.Link:
                continue
            # An internal link may live in /Dest (direct) or /A as a GoTo action.
            dest = annot.get(Name.Dest)
            if dest is None:
                action = annot.get(Name.A)
                if action is not None and action.get(Name.S) == Name.GoTo:
                    dest = action.get(Name.D)
            if dest is None:
                continue
            # Only string/name destinations represent a named anchor; arrays
            # are concrete page destinations and we leave those alone.
            if isinstance(dest, (pikepdf.String, pikepdf.Name)):
                dest_name = str(dest)
            else:
                continue
            embed_name = anchor_to_filename.get(dest_name)
            if not embed_name:
                continue
            new_action = pdf.make_indirect(Dictionary(
                Type=Name.Action,
                S=Name("/GoToE"),
                T=Dictionary(R=Name("/N"), N=String(embed_name)),
                D=Array([0, Name("/Fit")]),
                NewWindow=True,
            ))
            annot[Name.A] = new_action
            if Name.Dest in annot:
                del annot[Name.Dest]
            rewritten += 1

    log.info("Rewrote %d link annotations to open embedded attachments.", rewritten)
    pdf.save(str(pdf_path))
    return embedded, missing


# ---------------------------------------------------------------- CLI

def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    for noisy in ("weasyprint", "fontTools", "fontTools.subset", "fontTools.ttLib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(description="Render an itinerary markdown file to PDF.")
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--images", type=Path, default=Path("./images"))
    ap.add_argument("--attachments", type=Path, default=Path("./attachments"),
                    help="Directory with sub-PDFs referenced by [Attachment: ...].")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--section", type=str, default=None,
                    help="Render only sections whose title contains this substring.")
    ap.add_argument("--strict-images", action="store_true")
    ap.add_argument("--no-attachments", action="store_true",
                    help="Skip the pikepdf embedding step.")
    ap.add_argument("--html-only", action="store_true")
    args = ap.parse_args(argv)

    md_text = args.input.read_text(encoding="utf-8")
    itinerary = itinerary_parser.parse(md_text)
    log.info("Parsed %d sections from %s", len(itinerary.sections), args.input)

    itinerary = _filter_sections(itinerary, args.section)

    html, hits, misses = render_html(itinerary, args.images, args.strict_images)

    if misses:
        unique = sorted(set(misses))
        log.warning("Missing %d image refs (%d unique).", len(misses), len(unique))
        if args.strict_images:
            for n in unique:
                log.warning("  missing: %s", n)
            log.error("--strict-images set; refusing to write PDF.")
            return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)

    if args.html_only:
        args.out.write_text(html, encoding="utf-8")
        log.info("Wrote HTML to %s (%d bytes).", args.out, args.out.stat().st_size)
        return 0

    render_pdf(html, args.out)
    log.info("Wrote base PDF to %s (%d bytes).", args.out, args.out.stat().st_size)

    if not args.no_attachments:
        embedded, missing_atts = embed_attachments(args.out, itinerary, args.attachments)
        log.info("Embedded %d attachment(s); %d missing.", len(embedded), len(missing_atts))
        for label in missing_atts:
            log.warning("  attachment not found: %s", label)

    log.info(
        "Done. Images: %d resolved / %d missing. PDF size: %d bytes.",
        len(hits), len(set(misses)), args.out.stat().st_size,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
