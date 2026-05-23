# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""The .lantern bundle — a deck's files, zipped.

A .lantern is just a zip whose contents are a vanilla Marp project, so
unzipping it by hand yields something marp-cli renders identically:

    deck.md          the slides (fixed name)
    .marprc.yml      `themeSet: styles` — marp auto-loads this from the deck's
                     directory, so a hand-unzip registers the bundle's custom
                     themes exactly as the app does
    images/          referenced images
    styles/          custom theme CSS (and fonts)

The app never edits the zip in place: opening unpacks it to a temp working
directory — kept small so `marp --server` watches almost nothing — edits live
there, and Save re-zips. This module owns that pack/unpack/scaffold plumbing.

- new_working_dir(): a fresh temp directory under the app cache.
- scaffold(work_dir, deck_text): write the skeleton into a working dir.
- unpack(zip_path): extract a .lantern into a fresh working dir.
- pack(work_dir, zip_path): atomically zip a working dir into a .lantern.
- cleanup(work_dir): remove a working dir.
- display_name(zip_path): the bundle name with the .lantern suffix stripped.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

import base64
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

DECK_NAME = "deck.md"
# A bundle is a zip with a single .lantern extension. The plain .zip form was
# abandoned because its .zip suffix dragged in the uncapped application/zip
# glob, which a sandboxed app's glob can't outrank; .lantern sidesteps that.
SUFFIX = ".lantern"

# EPUB-style content marker: the first archive entry is an uncompressed file
# named `mimetype` whose bytes are MIME_TYPE.  A flatpak app can't register a
# glob weight high enough to beat application/zip's content magic, but a magic
# match on this fixed-offset marker wins (see nz.ursa.Lantern.mime.xml).
MIME_TYPE = "application/vnd.lantern+zip"
MIMETYPE_FILE = "mimetype"

# Where assets live inside a bundle. Fonts sit under styles/ so a custom theme
# CSS can @font-face them with a relative path that survives unzip.
IMAGES_DIR = "images"
FONTS_DIR = "styles/fonts"

# Bundle housekeeping metadata (title, author, dates). Kept out of the deck's
# frontmatter so saving never rewrites the slides. Marp ignores the file.
MANIFEST = "lantern.json"
_FORMAT = 1

# marp-cli auto-loads a .marprc.* from the deck's directory, so pointing
# themeSet at styles/ makes both the preview and a hand-unzip resolve the
# bundle's custom themes the same way. (marp only warns when styles/ is empty.)
_MARPRC = "themeSet: styles\n"

# Lantern's own theme. Assigning a font generates this CSS (a thin override
# layered on the deck's base theme) and points the deck's `theme:` directive
# at it.
THEME_FILE = "styles/lantern.css"
THEME_NAME = "lantern"

# Marp's built-in themes, always selectable without shipping any CSS.
BUILTIN_THEMES = ("default", "gaia", "uncover")

# Curated themes Lantern ships, as package data: themes/*.css next to this
# module, each carrying a `/* @theme name */` header. Picking one copies it
# into the bundle's styles/ so the deck stays portable after a hand-unzip.
THEMES_DIR = Path(__file__).resolve().parent / "themes"

# Typographic roles a bundled font can fill, mapped to the Marp slide selectors
# they style and a generic fallback if the embedded font fails to load.
FONT_ROLES = {
    "body": ("section", "sans-serif"),
    "headings": ("h1, h2, h3, h4, h5, h6", "sans-serif"),
    "monospace": ("code, pre, kbd, samp", "monospace"),
}
ROLE_LABELS = {"body": "Body", "headings": "Headings", "monospace": "Monospace"}

# Fonts are embedded as base64 data: URIs so a deck renders the same in the
# preview, in WebKit's PDF export, and after a hand-unzip — no path resolution.
_FONT_MIME = {".ttf": "font/ttf", ".otf": "font/otf",
              ".woff": "font/woff", ".woff2": "font/woff2"}
_FONT_FORMAT = {".ttf": "truetype", ".otf": "opentype",
                ".woff": "woff", ".woff2": "woff2"}


def _work_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    root = Path(base) / "lantern" / "work"
    root.mkdir(parents=True, exist_ok=True)
    return root


def new_working_dir() -> Path:
    """Create and return a fresh, empty working directory."""
    return Path(tempfile.mkdtemp(dir=_work_root()))


def scaffold(work_dir, deck_text: str) -> None:
    """Write a minimal bundle skeleton (deck + config + asset dirs)."""
    work_dir = Path(work_dir)
    (work_dir / DECK_NAME).write_text(deck_text, encoding="utf-8")
    (work_dir / ".marprc.yml").write_text(_MARPRC, encoding="utf-8")
    (work_dir / "images").mkdir(exist_ok=True)
    (work_dir / "styles").mkdir(exist_ok=True)
    write_meta(work_dir, {})


def unpack(zip_path) -> Path:
    """Extract `zip_path` into a fresh working dir; return its path.

    Raises ValueError if the archive isn't a Lantern bundle (no deck.md).
    """
    work_dir = new_working_dir()
    with zipfile.ZipFile(zip_path) as zf:
        _safe_extract(zf, work_dir)
    if not (work_dir / DECK_NAME).is_file():
        cleanup(work_dir)
        raise ValueError(f"{Path(zip_path).name} is not a Lantern bundle (no {DECK_NAME})")
    # Tolerate bundles that omitted the (possibly empty) asset dirs.
    (work_dir / "images").mkdir(exist_ok=True)
    (work_dir / "styles").mkdir(exist_ok=True)
    return work_dir


def pack(work_dir, zip_path) -> None:
    """Zip `work_dir`'s contents into `zip_path` atomically (temp + rename).

    The first entry is the uncompressed `mimetype` marker (see MIME_TYPE), so
    the archive is content-detectable as a Lantern bundle; the rest of the
    working dir follows. Any `mimetype` already in the working dir (from a
    previous unpack) is skipped so it isn't written twice.
    """
    work_dir = Path(work_dir)
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_name(zip_path.name + ".tmp")
    # strict_timestamps=False clamps any pre-1980 mtime (e.g. OSTree zeroes the
    # timestamps on flatpak-shipped files) up to 1980 instead of raising, so a
    # stray old mtime on a bundled asset can never crash a save.
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
        marker = zipfile.ZipInfo(MIMETYPE_FILE)
        marker.compress_type = zipfile.ZIP_STORED   # must be uncompressed
        zf.writestr(marker, MIME_TYPE.encode("ascii"))
        for p in sorted(work_dir.rglob("*")):
            rel = p.relative_to(work_dir).as_posix()
            if p.is_file() and rel != MIMETYPE_FILE:
                zf.write(p, rel)
    os.replace(tmp, zip_path)


def cleanup(work_dir) -> None:
    """Remove a working directory (best effort)."""
    shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Metadata — title, author, dates in lantern.json (housekeeping, not slides).
# ---------------------------------------------------------------------------
def read_meta(work_dir) -> dict:
    """Load the bundle manifest. Returns {} when it's absent or unreadable."""
    p = Path(work_dir) / MANIFEST
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_meta(work_dir, meta: dict) -> None:
    """Write the bundle manifest, always stamping the format version."""
    payload = {"format": _FORMAT}
    payload.update({k: v for k, v in meta.items() if k != "format"})
    (Path(work_dir) / MANIFEST).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Typography — map bundled fonts to roles, generating the Lantern theme CSS.
# Role assignments are the source of truth (in lantern.json under "fonts");
# styles/lantern.css is regenerated from them.
# ---------------------------------------------------------------------------
def font_roles(work_dir) -> dict:
    """The role -> bundle-relative font mapping, dropping fonts gone missing."""
    work_dir = Path(work_dir)
    fonts = read_meta(work_dir).get("fonts")
    if not isinstance(fonts, dict):
        return {}
    return {role: rel for role, rel in fonts.items()
            if role in FONT_ROLES and isinstance(rel, str) and (work_dir / rel).is_file()}


def set_font_role(work_dir, role: str, rel: Optional[str]) -> None:
    """Assign `rel` to `role` (or clear it when rel is None), then regenerate
    the theme CSS. No-op for an unknown role."""
    if role not in FONT_ROLES:
        return
    meta = read_meta(work_dir)
    fonts = meta.get("fonts")
    if not isinstance(fonts, dict):
        fonts = {}
    if rel:
        fonts[role] = rel
    else:
        fonts.pop(role, None)
    if fonts:
        meta["fonts"] = fonts
    else:
        meta.pop("fonts", None)
    write_meta(work_dir, meta)
    generate_theme(work_dir)


def base_theme(work_dir) -> str:
    """The deck's chosen base theme name (defaults to 'default').

    Stored in lantern.json under "baseTheme"; 'default' is the implicit base
    and kept out of the manifest. Never returns the managed lantern theme.
    """
    name = read_meta(work_dir).get("baseTheme")
    if isinstance(name, str) and name.strip() and name != THEME_NAME:
        return name
    return "default"


def set_base_theme(work_dir, name: str) -> None:
    """Record the chosen base theme, then regenerate the Lantern theme CSS.

    'default' is the implicit base, so it's stored as the absence of the key.
    Regenerating keeps styles/lantern.css importing the right base when fonts
    are assigned (and removes it when none are).
    """
    meta = read_meta(work_dir)
    if name and name != "default" and name != THEME_NAME:
        meta["baseTheme"] = name
    else:
        meta.pop("baseTheme", None)
    write_meta(work_dir, meta)
    generate_theme(work_dir)


def effective_theme(work_dir) -> str:
    """The `theme:` directive the deck should carry: the managed lantern theme
    when fonts are assigned (the generated override), else the base theme."""
    return THEME_NAME if font_roles(work_dir) else base_theme(work_dir)


def generate_theme(work_dir) -> None:
    """Write styles/lantern.css from the font-role assignments, or remove it.

    The generated theme is a thin override layer: it @imports the deck's base
    theme (see base_theme) and restates only the font-family rules, so the base
    theme's styling shows through with the chosen fonts on top. With no fonts
    assigned there's nothing to override, so the file is removed and the deck
    uses its base theme directly.
    """
    work_dir = Path(work_dir)
    target = work_dir / THEME_FILE
    roles = font_roles(work_dir)
    if not roles:
        if target.exists():
            target.unlink()
        return
    lines = ["/* @theme lantern */",
             "/* Generated by Lantern from the deck's font choices. */",
             f"@import '{base_theme(work_dir)}';", ""]
    families: dict = {}   # rel -> css family name (one @font-face per font file)
    for rel in dict.fromkeys(roles.values()):
        uri = _font_data_uri(work_dir / rel)
        if uri is None:
            continue
        fam = _font_family(rel)
        families[rel] = fam
        fmt = _FONT_FORMAT.get(Path(rel).suffix.lower(), "truetype")
        lines.append(f"@font-face {{ font-family: '{fam}'; "
                     f"src: url({uri}) format('{fmt}'); }}")
    lines.append("")
    for role, rel in roles.items():
        selector, fallback = FONT_ROLES[role]
        fam = families.get(rel)
        if fam:
            lines.append(f"{selector} {{ font-family: '{fam}', {fallback}; }}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_THEME_LINE_RE = re.compile(r"^theme:.*$", re.MULTILINE)


def set_theme_directive(deck_text: str, name: str) -> str:
    """Return `deck_text` with its frontmatter `theme:` set to `name`.

    Replaces an existing theme line, adds one to existing frontmatter, or
    prepends a minimal frontmatter block if the deck has none.
    """
    m = _FRONTMATTER_RE.match(deck_text)
    if not m:
        return f"---\nmarp: true\ntheme: {name}\n---\n\n{deck_text}"
    fm = m.group(1)
    if _THEME_LINE_RE.search(fm):
        new_fm = _THEME_LINE_RE.sub(f"theme: {name}", fm, count=1)
    else:
        new_fm = fm + f"\ntheme: {name}"
    return deck_text[:m.start(1)] + new_fm + deck_text[m.end(1):]


# ---------------------------------------------------------------------------
# Theme discovery — Marp builtins, curated themes Lantern ships, and the
# bundle's own custom CSS. available_themes() feeds the picker.
# ---------------------------------------------------------------------------
_THEME_HEADER_RE = re.compile(r"/\*\s*@theme\s+([\w-]+)\s*\*/")


def _theme_name(css_path) -> Optional[str]:
    """The theme name from a CSS file's `/* @theme name */` header, or None."""
    try:
        text = Path(css_path).read_text(encoding="utf-8")
    except OSError:
        return None
    m = _THEME_HEADER_RE.search(text)
    return m.group(1) if m else None


def list_curated_themes() -> list:
    """(name, path) for each curated theme Lantern ships, sorted by name."""
    if not THEMES_DIR.is_dir():
        return []
    found = [(name, p) for p in sorted(THEMES_DIR.glob("*.css"))
             if (name := _theme_name(p))]
    return sorted(found, key=lambda t: t[0])


def install_curated_theme(work_dir, name: str) -> Optional[str]:
    """Copy the curated theme `name` into the bundle's styles/ so it travels
    with the deck; return its theme name, or None when there's no such theme."""
    for theme_name, path in list_curated_themes():
        if theme_name == name:
            dest = Path(work_dir) / "styles" / path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            # write_bytes (not shutil.copy2) so the copy gets a current mtime
            # and normal, writable permissions rather than inheriting the
            # flatpak source file's zeroed timestamp and read-only mode.
            dest.write_bytes(path.read_bytes())
            return theme_name
    return None


def list_bundle_themes(work_dir) -> list:
    """Theme names of custom CSS already in the bundle's styles/, excluding the
    managed lantern theme, sorted."""
    styles = Path(work_dir) / "styles"
    if not styles.is_dir():
        return []
    names = {name for p in styles.glob("*.css")
             if (name := _theme_name(p)) and name != THEME_NAME}
    return sorted(names)


def available_themes(work_dir) -> list:
    """Ordered, de-duplicated theme choices for the picker.

    Each entry is {name, label, kind} with kind 'builtin' | 'curated' |
    'custom'. Marp builtins first, then curated themes Lantern ships, then any
    other custom themes already in the bundle. A name appears once; a curated
    theme already copied into the bundle stays labelled as curated.
    """
    seen: set = set()
    out: list = []

    def add(name, label, kind):
        if name not in seen:
            seen.add(name)
            out.append({"name": name, "label": label, "kind": kind})

    for name in BUILTIN_THEMES:
        add(name, name.title(), "builtin")
    for name, _path in list_curated_themes():
        add(name, name.title(), "curated")
    for name in list_bundle_themes(work_dir):
        add(name, f"{name} (in deck)", "custom")
    return out


def _font_family(rel: str) -> str:
    # CSS family name from the file stem; drop quotes that would break the rule.
    return Path(rel).stem.replace("'", "").replace('"', "")


def _font_data_uri(path) -> Optional[str]:
    path = Path(path)
    mime = _FONT_MIME.get(path.suffix.lower())
    if mime is None or not path.is_file():
        return None
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


# ---------------------------------------------------------------------------
# Assets — the resources manager copies images/fonts in and tracks usage.
# ---------------------------------------------------------------------------
def add_image(work_dir, src) -> str:
    """Copy `src` into images/, de-duping the name; return the bundle path."""
    return _add_asset(work_dir, src, IMAGES_DIR)


def add_font(work_dir, src) -> str:
    """Copy `src` into styles/fonts/, de-duping the name; return the bundle path."""
    return _add_asset(work_dir, src, FONTS_DIR)


def list_images(work_dir) -> list:
    """Bundle-relative paths of the files in images/, sorted."""
    return _list_dir(work_dir, IMAGES_DIR)


def list_fonts(work_dir) -> list:
    """Bundle-relative paths of the files in styles/fonts/, sorted."""
    return _list_dir(work_dir, FONTS_DIR)


def delete_asset(work_dir, rel) -> None:
    """Remove a bundle-relative asset (no-op if it's missing)."""
    p = Path(work_dir) / rel
    if p.is_file():
        p.unlink()


def count_references(deck_text: str, rel: str) -> int:
    """How many times the bundle-relative path appears in the deck text."""
    return deck_text.count(rel)


def _add_asset(work_dir, src, subdir) -> str:
    src = Path(src)
    dest_dir = Path(work_dir) / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = _dedupe_name(dest_dir, src.name)
    shutil.copy2(src, dest_dir / name)
    return f"{subdir}/{name}"


def _list_dir(work_dir, subdir) -> list:
    d = Path(work_dir) / subdir
    if not d.is_dir():
        return []
    return sorted(f"{subdir}/{p.name}" for p in d.iterdir() if p.is_file())


def _dedupe_name(dest_dir, name) -> str:
    # photo.png -> photo-1.png -> photo-2.png when the name is already taken.
    if not (dest_dir / name).exists():
        return name
    stem, ext = Path(name).stem, Path(name).suffix
    i = 1
    while (dest_dir / f"{stem}-{i}{ext}").exists():
        i += 1
    return f"{stem}-{i}{ext}"


def display_name(zip_path) -> str:
    """Bundle name for the title bar, with the .lantern suffix removed."""
    name = Path(zip_path).name
    return name[: -len(SUFFIX)] if name.endswith(SUFFIX) else Path(zip_path).stem


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract `zf` into `dest`, refusing any entry that escapes it (zip-slip)."""
    dest = dest.resolve()
    for member in zf.namelist():
        target = (dest / member).resolve()
        if not target.is_relative_to(dest):
            raise ValueError(f"unsafe path in archive: {member}")
    zf.extractall(dest)
