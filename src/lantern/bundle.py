# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""The .lantern.zip bundle — a deck's files, zipped.

A .lantern.zip is just a zip whose contents are a vanilla Marp project, so
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
- unpack(zip_path): extract a .lantern.zip into a fresh working dir.
- pack(work_dir, zip_path): atomically zip a working dir into a .lantern.zip.
- cleanup(work_dir): remove a working dir.
- display_name(zip_path): the bundle name with the .lantern.zip suffix stripped.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

DECK_NAME = "deck.md"
SUFFIX = ".lantern.zip"

# marp-cli auto-loads a .marprc.* from the deck's directory, so pointing
# themeSet at styles/ makes both the preview and a hand-unzip resolve the
# bundle's custom themes the same way. (marp only warns when styles/ is empty.)
_MARPRC = "themeSet: styles\n"


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
    """Zip `work_dir`'s contents into `zip_path` atomically (temp + rename)."""
    work_dir = Path(work_dir)
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = zip_path.with_name(zip_path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(work_dir.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(work_dir).as_posix())
    os.replace(tmp, zip_path)


def cleanup(work_dir) -> None:
    """Remove a working directory (best effort)."""
    shutil.rmtree(work_dir, ignore_errors=True)


def display_name(zip_path) -> str:
    """Bundle name for the title bar, with the .lantern.zip suffix removed."""
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
