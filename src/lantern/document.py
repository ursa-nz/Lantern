# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""In-memory model of the markdown file being edited.

- Document: tracks the on-disk path and the last-saved text so the window
  can detect dirty state and autosave only when content actually changed.
  Emits `path-changed` when the backing file moves.
- default_template: minimal frontmatter + heading used for `New` so a
  freshly created presentation isn't empty.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

import os
from pathlib import Path
from typing import Optional

from gi.repository import GObject


class Document(GObject.Object):
    """In-memory model of the markdown file being edited.

    Emits 'path-changed' when the backing file path changes (open, save-as).
    """

    __gsignals__ = {
        "path-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self._path: Optional[Path] = None
        # Snapshot of the text as last persisted to disk.  is_dirty()
        # compares the current editor buffer against this to decide
        # whether an autosave actually needs to write anything.
        self._last_saved: str = ""

    # ---------- properties ----------
    @property
    def path(self) -> Optional[Path]:
        return self._path

    @property
    def title(self) -> str:
        """File stem (no extension) for the headerbar title."""
        return self._path.stem if self._path else "Untitled"

    @property
    def subtitle(self) -> str:
        """Parent directory, with $HOME collapsed to ~ for compactness."""
        if not self._path:
            return ""
        try:
            home = str(Path.home())
            parent = str(self._path.parent)
            if parent.startswith(home):
                parent = "~" + parent[len(home):]
            return parent
        except Exception:
            return str(self._path.parent)

    # ---------- io ----------
    def load(self, path) -> str:
        """Read `path` and adopt it as the current document; return its text."""
        path = Path(path).expanduser().resolve()
        text = path.read_text(encoding="utf-8")
        self._path = path
        self._last_saved = text
        self.emit("path-changed")
        return text

    def save(self, text: str, path=None) -> None:
        """Write `text` to disk atomically.

        We write to a sibling .tmp file and rename it into place.  On
        POSIX, rename is atomic — a crash mid-save can't leave a half-
        written file where your slides used to be.  Pass `path` for
        save-as; omit it to overwrite the current file.
        """
        target = Path(path).expanduser().resolve() if path else self._path
        if target is None:
            raise ValueError("Document has no path; pass one to save().")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        path_changed = self._path != target
        self._path = target
        self._last_saved = text
        # Emit only when the path actually moved — the title bar will
        # update; ordinary in-place saves stay quiet.
        if path_changed:
            self.emit("path-changed")

    def is_dirty(self, current: str) -> bool:
        """True if `current` differs from what we last wrote to disk."""
        return current != self._last_saved


def default_template(title: str) -> str:
    """Minimal Marp frontmatter + heading used when creating a new file.

    Kept deliberately bare — no theme picker, no demo slides — so the
    starting deck doesn't carry editorial weight the user didn't ask for.
    """
    return f"""---
marp: true
paginate: true
---

# {title}

"""
