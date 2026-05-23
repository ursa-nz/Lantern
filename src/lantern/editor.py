# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""Editor pane — thin GtkSourceView 5 wrapper.

- Editor: markdown syntax highlighting, monospace font set via the
  `.lantern-editor` CSS class, and a `changed` signal forwarded from the
  underlying GtkSource.Buffer.
- _apply_scheme: follows Adw.StyleManager's light/dark preference and
  picks the matching GtkSource style scheme on change.

set_text() blocks the changed handler around the write so loading a file
into the buffer doesn't fire autosave.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

from gi.repository import Adw, GObject, Gtk, GtkSource


class Editor(GObject.Object):
    """GtkSourceView 5 wrapper with markdown highlighting and IBM Plex Mono.

    GtkSourceView keeps the data (Buffer) and the widget that draws it
    (View) as separate objects, so we hold both.  Emits 'changed' on
    every buffer mutation; callers debounce as needed.
    """

    # __gsignals__ declares the custom signals this GObject emits.  'changed'
    # fires on every buffer edit (the window debounces autosave on it);
    # 'cursor-moved' fires when the caret moves (drives preview slide sync).
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "cursor-moved": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()

        # The Buffer is the data model: text + syntax highlighting state.
        self.buffer = GtkSource.Buffer()
        lang = GtkSource.LanguageManager.get_default().get_language("markdown")
        if lang:
            self.buffer.set_language(lang)
        self.buffer.set_highlight_syntax(True)

        # The View is the widget that displays the Buffer on screen.
        self.view = GtkSource.View.new_with_buffer(self.buffer)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_auto_indent(True)
        self.view.set_indent_width(2)
        self.view.set_tab_width(2)
        self.view.set_insert_spaces_instead_of_tabs(True)
        self.view.set_show_line_numbers(False)
        # set_monospace toggles a generic mono fallback; we want a
        # specific face (IBM Plex Mono) so we pick the font in CSS
        # via the .lantern-editor class instead.
        self.view.set_monospace(False)
        self.view.set_pixels_above_lines(3)
        self.view.set_pixels_below_lines(3)
        self.view.set_left_margin(48)
        self.view.set_right_margin(48)
        self.view.set_top_margin(32)
        self.view.set_bottom_margin(32)
        self.view.add_css_class("lantern-editor")

        # Re-pick the GtkSource colour scheme whenever the system flips
        # between light and dark mode, so the editor follows Adwaita.
        style_mgr = Adw.StyleManager.get_default()
        style_mgr.connect("notify::dark", lambda mgr, *_: self._apply_scheme(mgr.get_dark()))
        self._apply_scheme(style_mgr.get_dark())

        # ScrolledWindow gives us scrollbars when the buffer grows past
        # the visible area; hexpand/vexpand let it fill the paned slot.
        scroller = Gtk.ScrolledWindow()
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_child(self.view)
        self.widget = scroller

        # Connect a bound method (not a lambda) so set_text() below can
        # temporarily block this exact callback by reference.
        self.buffer.connect("changed", self._on_buffer_changed)
        # The caret is the buffer's "insert" mark; its offset is the
        # cursor-position property, so notify fires whenever the caret moves.
        self.buffer.connect("notify::cursor-position",
                            lambda *_: self.emit("cursor-moved"))

    # ---------- text ----------
    def get_text(self) -> str:
        """Return the full buffer contents as a string."""
        start = self.buffer.get_start_iter()
        end = self.buffer.get_end_iter()
        return self.buffer.get_text(start, end, False)

    def get_cursor_line(self) -> int:
        """The 0-based line the caret is on."""
        it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        return it.get_line()

    def insert_at_cursor(self, text: str) -> None:
        """Insert `text` at the cursor. Fires 'changed', so the preview and
        autosave pick it up like any other edit."""
        self.buffer.insert_at_cursor(text)
        self.view.grab_focus()

    def set_text(self, text: str) -> None:
        """Replace buffer contents without re-firing 'changed'.

        We temporarily block our own handler so loading a file doesn't
        immediately schedule an autosave of the file we just loaded.
        """
        self.buffer.handler_block_by_func(self._on_buffer_changed)
        try:
            self.buffer.set_text(text)
        finally:
            self.buffer.handler_unblock_by_func(self._on_buffer_changed)

    # ---------- internals ----------
    def _on_buffer_changed(self, *_):
        # Forward GtkSource.Buffer's 'changed' as our own typed signal.
        self.emit("changed")

    def _apply_scheme(self, dark: bool) -> None:
        # Prefer the Adwaita-matching schemes; fall back to 'classic' if
        # the GtkSource version on this runtime doesn't ship them.
        mgr = GtkSource.StyleSchemeManager.get_default()
        candidates = ("Adwaita-dark", "classic-dark") if dark else ("Adwaita", "classic")
        for name in candidates:
            scheme = mgr.get_scheme(name)
            if scheme:
                self.buffer.set_style_scheme(scheme)
                return
