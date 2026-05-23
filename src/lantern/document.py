# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""In-memory model of the deck being edited, backed by a .lantern bundle.

A deck always lives in a working directory (see lantern.bundle): the editor
and preview read and write `deck.md` there, and Save re-zips the directory
into the bundle's .lantern. New decks and imported .md files start without
a bundle path until the first Save chooses one.

Two snapshots track two different kinds of "dirty":
- working text — what was last written to deck.md; drives the cheap autosave
  that keeps marp's live preview current.
- saved text — what was last re-zipped into the .lantern; drives the
  "unsaved changes" state in the title bar and on close.

- Document: owns the working dir, deck.md, and (once saved) the .lantern
  path. new()/open_bundle()/import_md() set things up; write_working() is the
  autosave; save() re-zips. Emits 'path-changed' when the bundle path moves.
- default_template: minimal frontmatter + heading for a fresh deck.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

from pathlib import Path
from typing import Optional

from gi.repository import GObject

from lantern import bundle


class Document(GObject.Object):
    """In-memory model of the deck being edited, backed by a working dir.

    Emits 'path-changed' when the backing .lantern changes (open, save-as).
    """

    __gsignals__ = {
        "path-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self._work_dir: Optional[Path] = None
        self._bundle_path: Optional[Path] = None   # the .lantern on disk
        self._import_name: Optional[str] = None     # title while still unsaved
        self._working_text: str = ""                # last written to deck.md
        self._saved_text: str = ""                  # last re-zipped to the bundle

    # ---------- properties ----------
    @property
    def work_dir(self) -> Optional[Path]:
        return self._work_dir

    @property
    def deck_path(self) -> Optional[Path]:
        return self._work_dir / bundle.DECK_NAME if self._work_dir else None

    @property
    def bundle_path(self) -> Optional[Path]:
        return self._bundle_path

    @property
    def is_saved(self) -> bool:
        """True once the deck has a .lantern on disk."""
        return self._bundle_path is not None

    @property
    def title(self) -> str:
        if self._bundle_path:
            return bundle.display_name(self._bundle_path)
        return self._import_name or "Untitled"

    @property
    def subtitle(self) -> str:
        """Bundle's parent dir (with $HOME collapsed), or 'Unsaved'."""
        if not self._bundle_path:
            return "Unsaved"
        parent = str(self._bundle_path.parent)
        home = str(Path.home())
        if parent.startswith(home):
            parent = "~" + parent[len(home):]
        return parent

    # ---------- lifecycle ----------
    def new(self, title: str) -> str:
        """Start a fresh, not-yet-saved deck in a new working dir; return text."""
        text = default_template(title)
        self._adopt(bundle.new_working_dir(), bundle_path=None,
                    import_name=title, deck_text=text, saved=False)
        return text

    def open_bundle(self, zip_path) -> str:
        """Unpack `zip_path` into a working dir and adopt it; return deck text.

        Raises ValueError if `zip_path` isn't a Lantern bundle.
        """
        zip_path = Path(zip_path).expanduser().resolve()
        work_dir = bundle.unpack(zip_path)   # may raise ValueError
        text = (work_dir / bundle.DECK_NAME).read_text(encoding="utf-8")
        self._adopt(work_dir, bundle_path=zip_path,
                    import_name=None, deck_text=text, saved=True)
        return text

    def import_md(self, md_path) -> str:
        """Wrap a loose .md into a fresh, unsaved bundle; return its text."""
        md_path = Path(md_path).expanduser().resolve()
        text = md_path.read_text(encoding="utf-8")
        work_dir = bundle.new_working_dir()
        bundle.scaffold(work_dir, text)
        self._adopt(work_dir, bundle_path=None,
                    import_name=md_path.stem, deck_text=text, saved=False,
                    already_scaffolded=True)
        return text

    # ---------- io ----------
    def write_working(self, text: str) -> None:
        """Autosave: write `text` to deck.md. Cheap; does NOT re-zip."""
        if self.deck_path is None or text == self._working_text:
            return
        self.deck_path.write_text(text, encoding="utf-8")
        self._working_text = text

    def save(self, text: str, path=None) -> None:
        """Write deck.md, then re-zip the working dir into the bundle.

        Pass `path` for Save As (a new .lantern); omit it to overwrite the
        current bundle. Raises ValueError if there's no path to save to.
        """
        if self._work_dir is None:
            raise ValueError("no working directory")
        target = Path(path).expanduser().resolve() if path else self._bundle_path
        if target is None:
            raise ValueError("Document has no bundle path; pass one to save().")
        self.write_working(text)
        bundle.pack(self._work_dir, target)
        moved = self._bundle_path != target
        self._bundle_path = target
        self._import_name = None
        self._saved_text = text
        if moved:
            self.emit("path-changed")

    def is_dirty(self, current: str) -> bool:
        """True if `current` differs from what was last saved to the bundle."""
        return current != self._saved_text

    def close(self) -> None:
        """Drop the working directory (call when the window closes)."""
        if self._work_dir:
            bundle.cleanup(self._work_dir)
        self._work_dir = None

    # ---------- internals ----------
    def _adopt(self, work_dir, *, bundle_path, import_name, deck_text, saved,
               already_scaffolded=False) -> None:
        # Switching decks abandons any previous working dir.
        if self._work_dir:
            bundle.cleanup(self._work_dir)
        self._work_dir = Path(work_dir)
        self._bundle_path = bundle_path
        self._import_name = import_name
        if not already_scaffolded and bundle_path is None:
            bundle.scaffold(self._work_dir, deck_text)
        self._working_text = deck_text
        # Unsaved decks have no on-disk bundle yet, so everything is "dirty".
        self._saved_text = deck_text if saved else ""
        self.emit("path-changed")


def default_template(title: str) -> str:
    """Minimal Marp frontmatter + heading used when creating a new deck.

    Kept deliberately bare — no demo slides — so the starting deck doesn't
    carry editorial weight the user didn't ask for.
    """
    return f"""---
marp: true
theme: default
paginate: true
---

# {title}

"""
