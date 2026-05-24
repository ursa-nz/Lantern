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

from gi.repository import Adw, Gdk, GObject, Gtk, GtkSource


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

        # A formatting bar above the editor, for Markdown beginners. The window
        # toggles its visibility from a preference via set_toolbar_visible.
        self._toolbar = self._build_toolbar()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self._toolbar)
        box.append(scroller)
        self.widget = box

        # Connect a bound method (not a lambda) so set_text() below can
        # temporarily block this exact callback by reference.
        self.buffer.connect("changed", self._on_buffer_changed)
        # The caret is the buffer's "insert" mark; its offset is the
        # cursor-position property, so notify fires whenever the caret moves.
        self.buffer.connect("notify::cursor-position",
                            lambda *_: self.emit("cursor-moved"))
        self._install_shortcuts()

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

    # ---------- formatting bar ----------
    def set_toolbar_visible(self, visible: bool) -> None:
        """Show or hide the formatting bar."""
        self._toolbar.set_visible(visible)

    def _install_shortcuts(self) -> None:
        # Editor-scoped formatting shortcuts. Capture phase so they fire before
        # GtkSourceView's own key handling.
        sc = Gtk.ShortcutController()
        sc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        specs = (
            ("<Control>b", lambda: self.wrap_selection("**")),
            ("<Control>i", lambda: self.wrap_selection("*")),
            ("<Control>e", lambda: self.wrap_selection("`")),
            ("<Control>k", self.insert_link),
        )
        for accel, fn in specs:
            sc.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(accel), self._callback_action(fn)))
        self.view.add_controller(sc)

        # Four Enters in a row, with nothing typed between, become a slide
        # break, so starting a new slide is a natural keystroke.
        self._enter_run = 0
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.view.add_controller(keys)

    @staticmethod
    def _callback_action(fn) -> "Gtk.CallbackAction":
        def cb(_widget, _args):
            fn()
            return True
        return Gtk.CallbackAction.new(cb)

    def _on_key_pressed(self, _ctrl, keyval, _code, state) -> bool:
        mods = state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                        | Gdk.ModifierType.SHIFT_MASK)
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not mods:
            self._enter_run += 1
            if self._enter_run >= 4:
                self._enter_run = 0
                self._enters_to_slide_break()
                return True   # swallow the fourth Enter
            return False
        self._enter_run = 0
        return False

    def _enters_to_slide_break(self) -> None:
        """Replace the run of just-typed blank lines with a slide break."""
        buf = self.buffer
        buf.begin_user_action()
        cur = buf.get_iter_at_mark(buf.get_insert())
        back = cur.copy()
        while back.backward_char():
            if back.get_char() == "\n":
                continue
            back.forward_char()
            break
        buf.delete(back, cur)
        buf.insert(back, "\n\n---\n\n")
        buf.place_cursor(back)
        buf.end_user_action()
        self.view.grab_focus()

    def _build_toolbar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2,
                      margin_top=4, margin_bottom=4, margin_start=6, margin_end=6)
        bar.add_css_class("toolbar")

        def icon_btn(icon, tip, callback):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_: callback())
            bar.append(b)

        def label_btn(label, tip, callback):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_: callback())
            bar.append(b)

        def sep():
            bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL,
                                     margin_start=4, margin_end=4))

        icon_btn("format-text-bold-symbolic", "Bold (Ctrl+B)", lambda: self.wrap_selection("**"))
        icon_btn("format-text-italic-symbolic", "Italic (Ctrl+I)", lambda: self.wrap_selection("*"))
        label_btn("Code", "Inline code (Ctrl+E)", lambda: self.wrap_selection("`"))
        sep()
        bar.append(self._heading_button())
        icon_btn("view-list-symbolic", "Bullet list", lambda: self.prefix_line("- "))
        icon_btn("format-indent-more-symbolic", "Quote", lambda: self.prefix_line("> "))
        sep()
        icon_btn("insert-link-symbolic", "Link (Ctrl+K)", self.insert_link)
        icon_btn("list-add-symbolic", "New slide", lambda: self.insert_at_cursor("\n\n---\n\n"))
        return bar

    def _heading_button(self) -> Gtk.MenuButton:
        btn = Gtk.MenuButton(label="Heading", tooltip_text="Heading level")
        btn.add_css_class("flat")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        for label, prefix in (("Title", "# "), ("Heading", "## "), ("Subheading", "### ")):
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.connect("clicked", lambda _w, p=prefix: self._heading_pick(p))
            box.append(b)
        self._heading_popover = Gtk.Popover()
        self._heading_popover.set_child(box)
        btn.set_popover(self._heading_popover)
        return btn

    def _heading_pick(self, prefix: str) -> None:
        self._heading_popover.popdown()
        self.set_heading(prefix)

    def wrap_selection(self, marker: str) -> None:
        """Toggle `marker` around the selection, or the word under the cursor.

        If the target is already wrapped (the markers are selected, or sit just
        outside it) the markers are removed. With no selection the word under
        the cursor is used; on empty space an empty pair is inserted with the
        cursor between.
        """
        buf = self.buffer
        buf.begin_user_action()
        # get_selection_bounds() returns (start, end) with a selection, or an
        # empty tuple without one. Fall back to the word under the cursor.
        bounds = buf.get_selection_bounds()
        if bounds:
            s_off, e_off = bounds[0].get_offset(), bounds[1].get_offset()
        else:
            start, end = self._word_bounds()
            s_off, e_off = start.get_offset(), end.get_offset()
        n = len(marker)
        inner = self._text_between(s_off, e_off)
        if len(inner) >= 2 * n and inner.startswith(marker) and inner.endswith(marker):
            self._replace(s_off, e_off, inner[n:-n])        # markers were selected
        elif inner and self._surrounded_by(s_off, e_off, marker):
            self._replace(s_off - n, e_off + n, inner)       # markers sit just outside
        elif inner:
            self._replace(s_off, e_off, f"{marker}{inner}{marker}")
        else:
            buf.insert_at_cursor(marker + marker)
            cur = buf.get_iter_at_mark(buf.get_insert())
            cur.backward_chars(n)
            buf.place_cursor(cur)
        buf.end_user_action()
        self.view.grab_focus()

    def _text_between(self, s_off: int, e_off: int) -> str:
        buf = self.buffer
        return buf.get_text(buf.get_iter_at_offset(s_off),
                            buf.get_iter_at_offset(e_off), False)

    def _replace(self, s_off: int, e_off: int, text: str) -> None:
        buf = self.buffer
        buf.delete(buf.get_iter_at_offset(s_off), buf.get_iter_at_offset(e_off))
        buf.insert(buf.get_iter_at_offset(s_off), text)

    def _word_bounds(self):
        """Iters around the word under the cursor, or the cursor twice when it
        sits on whitespace."""
        cur = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        start, end = cur.copy(), cur.copy()
        if cur.inside_word() or cur.starts_word() or cur.ends_word():
            if not start.starts_word():
                start.backward_word_start()
            if not end.ends_word():
                end.forward_word_end()
        return start, end

    def _surrounded_by(self, s_off: int, e_off: int, marker: str) -> bool:
        """True if `marker` sits immediately outside [s_off, e_off] and isn't
        part of a longer run, so italic '*' doesn't match a bold '**'."""
        n = len(marker)
        if s_off < n:
            return False
        if self._text_between(s_off - n, s_off) != marker:
            return False
        if self._text_between(e_off, e_off + n) != marker:
            return False
        ch = marker[0]
        if s_off - n > 0 and self._text_between(s_off - n - 1, s_off - n) == ch:
            return False
        if self._text_between(e_off + n, e_off + n + 1) == ch:
            return False
        return True

    def prefix_line(self, prefix: str) -> None:
        """Insert `prefix` at the start of the cursor's line."""
        buf = self.buffer
        buf.begin_user_action()
        line_start = buf.get_iter_at_mark(buf.get_insert())
        line_start.set_line_offset(0)
        buf.insert(line_start, prefix)
        buf.end_user_action()
        self.view.grab_focus()

    def set_heading(self, prefix: str) -> None:
        """Set the heading level on the cursor's line.

        Any existing `#` marker is replaced, so picking a level swaps it rather
        than stacking more hashes and shrinking the heading.
        """
        buf = self.buffer
        buf.begin_user_action()
        it = buf.get_iter_at_mark(buf.get_insert())
        it.set_line_offset(0)
        line_start = it.get_offset()
        line_end = it.copy()
        if not line_end.ends_line():
            line_end.forward_to_line_end()
        line_text = buf.get_text(it, line_end, False)
        hashes = len(line_text) - len(line_text.lstrip("#"))
        if 0 < hashes <= 6 and line_text[hashes:hashes + 1] == " ":
            buf.delete(buf.get_iter_at_offset(line_start),
                       buf.get_iter_at_offset(line_start + hashes + 1))
        buf.insert(buf.get_iter_at_offset(line_start), prefix)
        buf.end_user_action()
        self.view.grab_focus()

    def insert_link(self) -> None:
        """Prompt for a title and link, then insert a Markdown link.

        A current selection prefills the title and is replaced on insert.
        """
        buf = self.buffer
        bounds = buf.get_selection_bounds()
        if bounds:
            sel_text = buf.get_text(bounds[0], bounds[1], False)
            span = (bounds[0].get_offset(), bounds[1].get_offset())
        else:
            sel_text, span = "", None

        group = Adw.PreferencesGroup()
        title_row = Adw.EntryRow(title="Title")
        title_row.set_text(sel_text)
        url_row = Adw.EntryRow(title="Link")
        group.add(title_row)
        group.add(url_row)

        dlg = Adw.AlertDialog(heading="Insert link")
        dlg.set_extra_child(group)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("insert", "Insert")
        dlg.set_response_appearance("insert", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("insert")
        dlg.set_close_response("cancel")
        dlg.connect("response", self._on_link_response, title_row, url_row, span)
        dlg.present(self.view.get_root())

    def _on_link_response(self, _dlg, response, title_row, url_row, span) -> None:
        if response != "insert":
            return
        title = title_row.get_text().strip() or "text"
        url = url_row.get_text().strip() or "url"
        buf = self.buffer
        buf.begin_user_action()
        if span is not None:
            buf.delete(buf.get_iter_at_offset(span[0]), buf.get_iter_at_offset(span[1]))
            buf.insert(buf.get_iter_at_offset(span[0]), f"[{title}]({url})")
        else:
            buf.insert_at_cursor(f"[{title}]({url})")
        buf.end_user_action()
        self.view.grab_focus()

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
