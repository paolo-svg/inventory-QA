"""Parse the itinerary markdown into a structured tree.

Source format conventions (see itineraries/*.md):
  # <Trip title>           -> trip title
  ## <Heading>             -> top-level block (Introduction, Recommendation Items, etc.)
  ### C/ <Date>: <Title>   -> one itinerary item
  [Image: <filename>]      -> image reference, resolved against an images directory
  [Gallery: <name>]        -> labeled gallery; following [Image: ...] lines belong to it
  [Attachment: <name>]     -> attachment pill
  Hero image: [Image: x]   -> document hero image (appears in front-matter)
  Member: <name>           -> member, from front-matter

Inside a section, content is captured as an ordered `blocks` list so the
renderer can interleave text and images in source order. This is what fixes
multi-day cruise sections like Ecoventura where images for each day need
to sit under their own day heading rather than bunched at the end.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from markdown_it import MarkdownIt

log = logging.getLogger(__name__)

IMAGE_RE = re.compile(r"^\[Image:\s*(.+?)\]\s*$")
GALLERY_RE = re.compile(r"^\[Gallery:\s*(.+?)\]\s*$")
ATTACHMENT_RE = re.compile(r"^\[Attachment:\s*(.+?)\]\s*$")
SECTION_RE = re.compile(r"^###\s+C/\s*(.+)$")
H1_RE = re.compile(r"^#\s+(.+)$")
H2_RE = re.compile(r"^##\s+(.+)$")
FRONTMATTER_KV_RE = re.compile(r"^([A-Za-z][\w \-]*):\s*(.+)$")

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


@dataclass
class Gallery:
    name: str
    images: list[str] = field(default_factory=list)


# --- Ordered content blocks ----------------------------------------------

@dataclass
class TextBlock:
    html: str


@dataclass
class ImagesBlock:
    names: list[str] = field(default_factory=list)


@dataclass
class GalleryBlock:
    name: str
    images: list[str] = field(default_factory=list)


@dataclass
class AttachmentBlock:
    name: str


Block = Union[TextBlock, ImagesBlock, GalleryBlock, AttachmentBlock]


@dataclass
class Section:
    raw_header: str
    date: str
    title: str
    blocks: list[Block] = field(default_factory=list)

    # Convenience views over `blocks` — kept so the pikepdf attachment-embedding
    # path in generate.py keeps working without changes.
    @property
    def images(self) -> list[str]:
        out: list[str] = []
        for b in self.blocks:
            if isinstance(b, ImagesBlock):
                out.extend(b.names)
        return out

    @property
    def galleries(self) -> list[Gallery]:
        return [Gallery(name=b.name, images=list(b.images))
                for b in self.blocks if isinstance(b, GalleryBlock)]

    @property
    def attachments(self) -> list[str]:
        return [b.name for b in self.blocks if isinstance(b, AttachmentBlock)]


@dataclass
class Itinerary:
    title: str = ""
    member: str = ""
    thread: str = ""
    hero_image: Optional[str] = None
    intro_html: str = ""
    sections: list[Section] = field(default_factory=list)


def _md_to_html(md_text: str) -> str:
    md = MarkdownIt("commonmark", {"breaks": True, "linkify": True})
    return md.render(md_text.strip())


def _split_front_matter(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    """Pull leading `Key: value` lines (and an `---` divider) off the top."""
    meta: dict[str, str] = {}
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if s == "---":
            i += 1
            break
        m = FRONTMATTER_KV_RE.match(s)
        if not m:
            break
        meta[m.group(1).strip().lower()] = m.group(2).strip()
        i += 1
    return meta, lines[i:]


def parse(md_text: str) -> Itinerary:
    lines = md_text.splitlines()
    meta, lines = _split_front_matter(lines)

    itinerary = Itinerary()
    itinerary.member = meta.get("member", "")
    itinerary.thread = meta.get("thread", "")
    hero = meta.get("hero image", "")
    if hero:
        m = IMAGE_RE.match(hero)
        itinerary.hero_image = m.group(1).strip() if m else hero

    current_section: Optional[Section] = None
    current_gallery: Optional[GalleryBlock] = None
    text_buf: list[str] = []
    intro_buf: list[str] = []
    in_section = False

    def flush_text():
        nonlocal text_buf
        if not current_section:
            return
        text = "\n".join(text_buf).strip()
        if text:
            current_section.blocks.append(TextBlock(html=_md_to_html(text)))
        text_buf = []

    def last_images_block() -> Optional[ImagesBlock]:
        """If the section's last block is an open ImagesBlock, return it so
        consecutive [Image:] lines coalesce into one row."""
        if current_section and current_section.blocks:
            tail = current_section.blocks[-1]
            if isinstance(tail, ImagesBlock):
                return tail
        return None

    for raw in lines:
        line = raw.rstrip()

        m_h1 = H1_RE.match(line)
        if m_h1:
            itinerary.title = m_h1.group(1).strip()
            continue

        m_section = SECTION_RE.match(line)
        if m_section:
            if current_section:
                flush_text()
                itinerary.sections.append(current_section)
            elif intro_buf:
                itinerary.intro_html = _md_to_html("\n".join(intro_buf))
                intro_buf = []
            header = m_section.group(1).strip()
            if ":" in header:
                date_part, title_part = header.split(":", 1)
            else:
                date_part, title_part = "", header
            current_section = Section(
                raw_header=header,
                date=date_part.strip(),
                title=title_part.strip(),
            )
            current_gallery = None
            in_section = True
            continue

        m_h2 = H2_RE.match(line)
        if m_h2 and not in_section:
            intro_buf.append(line)
            continue

        m_img = IMAGE_RE.match(line)
        if m_img:
            name = m_img.group(1).strip()
            if current_section:
                flush_text()
                if current_gallery is not None:
                    current_gallery.images.append(name)
                else:
                    block = last_images_block()
                    if block is None:
                        block = ImagesBlock()
                        current_section.blocks.append(block)
                    block.names.append(name)
            continue

        m_gal = GALLERY_RE.match(line)
        if m_gal:
            if current_section:
                flush_text()
                current_gallery = GalleryBlock(name=m_gal.group(1).strip())
                current_section.blocks.append(current_gallery)
            continue

        m_att = ATTACHMENT_RE.match(line)
        if m_att:
            if current_section:
                flush_text()
                current_section.blocks.append(AttachmentBlock(name=m_att.group(1).strip()))
            continue

        # Any non-image, non-gallery line closes an open gallery.
        if current_gallery is not None and line.strip():
            current_gallery = None

        if current_section:
            text_buf.append(line)
        else:
            intro_buf.append(line)

    if current_section:
        flush_text()
        itinerary.sections.append(current_section)
    elif intro_buf and not itinerary.intro_html:
        itinerary.intro_html = _md_to_html("\n".join(intro_buf))

    return itinerary


def resolve_image(name: str, images_dir: Path) -> Optional[Path]:
    """Find a real file on disk for a given image reference.

    Tries: exact name, case-insensitive match, and extension-agnostic match
    (the source occasionally references files without extensions).
    """
    if not images_dir.exists():
        return None

    direct = images_dir / name
    if direct.is_file():
        return direct

    stem_target = Path(name).stem.lower()
    given_ext = Path(name).suffix.lower()

    for entry in images_dir.iterdir():
        if not entry.is_file():
            continue
        entry_name = entry.name.lower()
        if entry_name == name.lower():
            return entry
        entry_stem = entry.stem.lower()
        entry_ext = entry.suffix.lower()
        if entry_stem == stem_target:
            if not given_ext or given_ext == entry_ext or entry_ext in IMAGE_EXTS:
                return entry
    return None
