"""Aurora itinerary PDF generator — Claude skill entrypoint.

Self-contained: bundles its own templates, fonts, and brand assets in
sibling directories. Designed to be invoked from .claude/skills/aurora-
itinerary/scripts/ either directly (`python generate.py ...`) or as a
module (`python -m scripts.generate ...`).

Typical flow:

  python scripts/generate.py \\
      --input  /tmp/work/itinerary.md \\
      --images /tmp/work/images \\
      --attachments /tmp/work/attachments \\
      --out    /tmp/work/itinerary.pdf \\
      --compress

The --compress flag post-processes the final PDF through ghostscript's
/ebook profile, which typically drops the size by ~10x with no visible
loss at A4 viewing size.
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

# Self-contained imports — works when invoked as `python generate.py` or
# `python -m scripts.generate`.
if __package__:
    from . import parser as itinerary_parser
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import parser as itinerary_parser  # type: ignore

log = logging.getLogger("aurora-itinerary")

SKILL_DIR = Path(__file__).resolve().parent.parent  # .claude/skills/aurora-itinerary/
TEMPLATES_DIR = SKILL_DIR / "templates"
ASSETS_DIR = SKILL_DIR / "assets"
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
        urls = [u for u in (resolve(n) for n in names) if u][:3]
        if not urls:
            return Markup("")
        klass = {1: "one", 2: "two", 3: "three"}[len(urls)]
        parts = [f'<div class="img-row {klass}">']
        for u in urls:
            parts.append(f'<img src="{u}" />')
        parts.append("</div>")
        return Markup("".join(parts))

    return render


def _extract_dates(itinerary) -> str:
    if not itinerary.intro_html:
        return ""
    text = re.sub(r"<[^>]+>", "\n", itinerary.intro_html)
    for line in text.splitlines():
        line = line.strip()
        if re.search(r"\b\d{4}\b", line) and "-" in line:
            return line
    return ""


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
        wordmark_dark=_file_uri(ASSETS_DIR / "aurora_wordmark_dark.png"),
        wordmark_light=_file_uri(ASSETS_DIR / "aurora_wordmark_light.png"),
    )
    return html, hits, misses


def render_pdf(html: str, out_path: Path) -> None:
    from weasyprint import HTML
    HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(str(out_path))


# ------------------------------------------------------------- Attachments

def _find_attachment_file(name: str, directory: Path) -> Optional[Path]:
    if not directory.exists():
        return None
    target_slug = _slug(name)
    candidates = [p for p in directory.iterdir() if p.is_file()]
    for p in candidates:
        if p.stem.lower() == name.lower():
            return p
    for p in candidates:
        if _slug(p.stem) == target_slug:
            return p
    for p in candidates:
        if target_slug in _slug(p.stem) or _slug(p.stem) in target_slug:
            return p
    return None


def embed_attachments(
    pdf_path: Path,
    itinerary,
    attachments_dir: Path,
) -> tuple[list[tuple[str, Path]], list[str]]:
    import pikepdf
    from pikepdf import Name, Dictionary, Array, String

    refs: list[tuple[str, str]] = []
    for section in itinerary.sections:
        for att in section.attachments:
            refs.append((att, attachment_anchor(att)))
    if not refs:
        return [], []

    embedded: list[tuple[str, Path]] = []
    missing: list[str] = []

    pdf = pikepdf.open(str(pdf_path), allow_overwriting_input=True)

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

    rewritten = 0
    for page in pdf.pages:
        annots = page.get(Name.Annots)
        if annots is None:
            continue
        for annot in annots:
            if annot.get(Name.Subtype) != Name.Link:
                continue
            dest = annot.get(Name.Dest)
            if dest is None:
                action = annot.get(Name.A)
                if action is not None and action.get(Name.S) == Name.GoTo:
                    dest = action.get(Name.D)
            if dest is None:
                continue
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


# ------------------------------------------------------------- Compression

def compress_pdf(src: Path, dst: Path, dpi: int = 150) -> None:
    """Run ghostscript /ebook with image downsampling for a print-quality
    PDF that's typically ~10x smaller than the WeasyPrint output."""
    if not shutil.which("gs"):
        log.warning("ghostscript not installed; skipping compression. Install with `apt-get install ghostscript` or `brew install ghostscript`.")
        shutil.copy2(src, dst)
        return
    cmd = [
        "gs",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.5",
        "-dPDFSETTINGS=/ebook",
        "-dDownsampleColorImages=true",
        f"-dColorImageResolution={dpi}",
        "-dColorImageDownsampleType=/Bicubic",
        "-dDownsampleGrayImages=true",
        f"-dGrayImageResolution={dpi}",
        "-dDetectDuplicateImages=true",
        "-dNOPAUSE", "-dQUIET", "-dBATCH",
        f"-sOutputFile={dst}",
        str(src),
    ]
    subprocess.run(cmd, check=True)


# ------------------------------------------------------------- CLI

def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    for noisy in ("weasyprint", "fontTools", "fontTools.subset", "fontTools.ttLib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(description="Render an Aurora itinerary markdown file to a branded PDF.")
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--images", type=Path, default=Path("./images"))
    ap.add_argument("--attachments", type=Path, default=Path("./attachments"))
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--strict-images", action="store_true",
                    help="Fail if any [Image: ...] reference can't be resolved on disk.")
    ap.add_argument("--no-attachments", action="store_true")
    ap.add_argument("--html-only", action="store_true")
    ap.add_argument("--compress", action="store_true",
                    help="Post-process with ghostscript /ebook to shrink final size.")
    args = ap.parse_args(argv)

    md_text = args.input.read_text(encoding="utf-8")
    itinerary = itinerary_parser.parse(md_text)
    log.info("Parsed %d sections from %s", len(itinerary.sections), args.input)

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
        return 0

    # Render to a working path so we can swap in the compressed version cleanly.
    work_pdf = args.out.with_suffix(".full.pdf") if args.compress else args.out
    render_pdf(html, work_pdf)
    log.info("Wrote base PDF to %s (%d bytes).", work_pdf, work_pdf.stat().st_size)

    if not args.no_attachments:
        embedded, missing_atts = embed_attachments(work_pdf, itinerary, args.attachments)
        log.info("Embedded %d attachment(s); %d missing.", len(embedded), len(missing_atts))
        for label in missing_atts:
            log.warning("  attachment not found: %s", label)

    if args.compress:
        compress_pdf(work_pdf, args.out)
        work_pdf.unlink(missing_ok=True)
        log.info("Compressed PDF: %d bytes.", args.out.stat().st_size)

    log.info(
        "Done. Images: %d resolved / %d missing. Final size: %d bytes.",
        len(hits), len(set(misses)), args.out.stat().st_size,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
