"""CLI: render an itinerary markdown file into a multimedia PDF.

System dependencies for WeasyPrint:
  Debian/Ubuntu:  apt-get install libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libffi-dev
  macOS:          brew install pango cairo gdk-pixbuf libffi

Usage:
  python -m pdf_itinerary.generate \\
      --input itineraries/errol_amazon_galapagos.md \\
      --images ./images \\
      --out errol_itinerary.pdf
"""

from __future__ import annotations

import argparse
import logging
import sys
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
        urls = [resolve(n) for n in names]
        urls = [u for u in urls if u]
        if not urls:
            return Markup("")
        count = len(urls)
        if count == 1:
            klass = "one"
        elif count == 2:
            klass = "two"
        elif count == 3:
            klass = "three"
        else:
            klass = "many"
        parts = [f'<div class="img-grid {klass}">']
        for u in urls:
            parts.append(f'<img src="{u}" />')
        parts.append("</div>")
        return Markup("".join(parts))

    return render


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
        styles_href=_file_uri(TEMPLATES_DIR / "styles.css"),
        render_image_grid=render_image_grid,
    )
    return html, hits, misses


def build_pdf(html: str, out_path: Path) -> None:
    # Imported lazily so `--help` works without WeasyPrint's system deps.
    from weasyprint import HTML

    HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(str(out_path))


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    for noisy in ("weasyprint", "fontTools", "fontTools.subset", "fontTools.ttLib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(description="Render an itinerary markdown file to PDF.")
    ap.add_argument("--input", required=True, type=Path, help="Path to itinerary markdown.")
    ap.add_argument("--images", type=Path, default=Path("./images"), help="Directory of images.")
    ap.add_argument("--out", required=True, type=Path, help="Output PDF path.")
    ap.add_argument("--strict-images", action="store_true", help="Fail if any image is missing.")
    ap.add_argument("--html-only", action="store_true", help="Write HTML instead of PDF (debug).")
    args = ap.parse_args(argv)

    md_text = args.input.read_text(encoding="utf-8")
    itinerary = itinerary_parser.parse(md_text)
    log.info("Parsed %d sections from %s", len(itinerary.sections), args.input)

    html, hits, misses = render_html(itinerary, args.images, args.strict_images)

    if misses:
        unique_misses = sorted(set(misses))
        log.warning("Missing %d image references (%d unique).", len(misses), len(unique_misses))
        for name in unique_misses:
            log.warning("  missing: %s", name)
        if args.strict_images:
            log.error("--strict-images set; refusing to write PDF.")
            return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)

    if args.html_only:
        args.out.write_text(html, encoding="utf-8")
        log.info("Wrote HTML to %s (%d bytes)", args.out, args.out.stat().st_size)
        return 0

    build_pdf(html, args.out)
    log.info(
        "Wrote PDF to %s (%d bytes). Images: %d resolved, %d missing.",
        args.out, args.out.stat().st_size, len(hits), len(misses),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
