# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""Main window: header, layout toggle, present modes, export, autosave.

- LanternWindow: the Adw.ApplicationWindow.  Owns the Document, the
  MarpServer, the Editor and Preview, and wires the three together.
- action_new_file / action_open_file / load_file: file lifecycle.
- action_export: opens a save dialog, then hands off to lantern.export to
  produce HTML (marp), PDF (WebKit print), or PPTX (pandoc).
- action_present_toggle: enters/exits present mode.  windowed=True gives
  a borderless floating window (Zoom-friendly); windowed=False fullscreens.
- _autosave: debounced write-back so marp's live-reload picks changes up
  without the user pressing save.

State persisted to ~/.config/lantern/state.json across runs is limited to
the last-used folder for file dialogs.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

import json
import os
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from lantern import APP_ID, APP_NAME, export
from lantern.document import Document, default_template
from lantern.editor import Editor
from lantern.marp_server import MarpServer
from lantern.preview import Preview


EXPORT_FORMATS = (
    # (action suffix, menu label, file extension)
    ("html", "HTML",               "html"),
    ("pdf",  "PDF",                "pdf"),
    ("pptx", "PowerPoint (.pptx)", "pptx"),
)


LAYOUT_EDITOR  = "editor"
LAYOUT_SPLIT   = "split"
LAYOUT_PREVIEW = "preview"

AUTOSAVE_DEBOUNCE_MS = 400


def _config_dir() -> Path:
    """Return ~/.config/lantern, creating it if needed.  Honours XDG."""
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "lantern"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_state() -> dict:
    """Load persistent UI state.  Returns {} if the file is missing or corrupt."""
    p = _config_dir() / "state.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    """Persist UI state.  Failures are swallowed — state is nice-to-have."""
    p = _config_dir() / "state.json"
    try:
        p.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


class LanternWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title(APP_NAME)
        self.set_default_size(1200, 760)
        self.set_size_request(720, 480)
        self.set_icon_name(APP_ID)

        # The window owns its document model, preview-server controller,
        # and the two view widgets.  Nothing here is shared across windows.
        self.document = Document()
        self.document.connect("path-changed", self._on_path_changed)

        self.marp = MarpServer()
        self.editor = Editor()
        self.preview = Preview()

        self._state = _load_state()
        self._save_timeout = 0
        self._layout = LAYOUT_SPLIT

        self._build_ui()
        self._install_shortcuts()
        self.editor.connect("changed", self._on_editor_changed)
        self.connect("close-request", self._on_close_request)
        # Reset chrome if fullscreen is exited via the window manager
        # (e.g. user hits Super+Up) rather than our own Escape handler.
        self.connect("notify::fullscreened", self._on_fullscreen_changed)

        # Present-mode state.  `_presenting` covers both fullscreen and
        # windowed present so Escape exits either one; the `_pre_*`
        # snapshots let _exit_present restore exactly what was showing
        # before — leaving feels like nothing happened.
        self._pre_present_layout: str | None = None
        self._presenting: bool = False
        self._pre_present_decorated: bool = True

        self._show_welcome()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self._title = Adw.WindowTitle.new(APP_NAME, "")
        self._header = Adw.HeaderBar()
        self._header.set_title_widget(self._title)
        self._header.pack_start(self._build_layout_toggle())
        self._header.pack_start(self._build_present_button())
        self._header.pack_end(self._build_menu_button())

        # Toast overlay wraps the content stack so toasts float over
        # whichever page is visible (welcome or doc).
        self._toast_overlay = Adw.ToastOverlay()

        self._content_stack = Gtk.Stack()
        self._content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._content_stack.set_transition_duration(180)
        self._toast_overlay.set_child(self._content_stack)

        self._content_stack.add_named(self._build_welcome(), "welcome")
        self._content_stack.add_named(self._build_doc(),     "doc")

        # ToolbarView keeps the headerbar fixed at the top and gives us
        # a clean way to hide it (set_reveal_top_bars) for present mode.
        self._toolbar_view = Adw.ToolbarView()
        self._toolbar_view.add_top_bar(self._header)
        self._toolbar_view.set_content(self._toast_overlay)
        self.set_content(self._toolbar_view)

    def _build_layout_toggle(self) -> Gtk.Widget:
        self._btn_editor  = Gtk.ToggleButton()
        self._btn_split   = Gtk.ToggleButton()
        self._btn_preview = Gtk.ToggleButton()

        for btn, icon, tip in (
            (self._btn_editor,  "accessories-text-editor-symbolic", "Editor"),
            (self._btn_split,   "view-dual-symbolic",               "Split"),
            (self._btn_preview, "x-office-presentation-symbolic",   "Preview"),
        ):
            btn.set_icon_name(icon)
            btn.set_tooltip_text(tip)

        # Sharing the same group makes the three buttons mutually
        # exclusive — exactly one is active at any time.
        self._btn_split.set_group(self._btn_editor)
        self._btn_preview.set_group(self._btn_editor)
        self._btn_split.set_active(True)

        for btn, mode in (
            (self._btn_editor,  LAYOUT_EDITOR),
            (self._btn_split,   LAYOUT_SPLIT),
            (self._btn_preview, LAYOUT_PREVIEW),
        ):
            btn.connect("toggled", self._on_layout_toggled, mode)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        # The "linked" CSS class turns adjacent buttons into a segmented
        # control without a gap.
        box.add_css_class("linked")
        box.append(self._btn_editor)
        box.append(self._btn_split)
        box.append(self._btn_preview)
        return box

    def _build_present_button(self) -> Gtk.Widget:
        # Two buttons: borderless windowed (for Zoom-style screenshare)
        # and fullscreen.  Both leave on Escape.  Bundled into a Box so
        # they sit together to the right of the layout toggle.
        windowed = Gtk.Button()
        windowed.set_icon_name("view-restore-symbolic")
        windowed.set_tooltip_text("Present in window (borderless)")
        windowed.set_action_name("win.present-windowed")

        fullscreen = Gtk.Button()
        fullscreen.set_icon_name("view-fullscreen-symbolic")
        fullscreen.set_tooltip_text("Present (F5)")
        fullscreen.set_action_name("win.present")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        box.set_margin_start(6)
        box.append(windowed)
        box.append(fullscreen)
        return box

    def _build_menu_button(self) -> Gtk.MenuButton:
        # Gio.Menu builds the model; sections render with separators
        # between them.  Actions are referenced by name — the actual
        # callbacks are wired in _install_shortcuts / LanternApp.
        menu = Gio.Menu()
        file_section = Gio.Menu()
        file_section.append("New",       "app.new")
        file_section.append("Open…", "app.open")
        menu.append_section(None, file_section)
        view_section = Gio.Menu()
        export_menu = Gio.Menu()
        for suffix, label, _ext in EXPORT_FORMATS:
            export_menu.append(label, f"win.export-{suffix}")
        view_section.append_submenu("Export…", export_menu)
        menu.append_section(None, view_section)
        meta_section = Gio.Menu()
        meta_section.append(f"About {APP_NAME}", "app.about")
        menu.append_section(None, meta_section)

        btn = Gtk.MenuButton()
        btn.set_icon_name("open-menu-symbolic")
        btn.set_menu_model(menu)
        btn.set_primary(True)
        return btn

    # ------------------------------------------------------------------
    # Shortcuts + present mode
    # ------------------------------------------------------------------
    def _install_shortcuts(self) -> None:
        """Register window-scoped actions and keyboard shortcuts.

        Present and export actions start disabled and are enabled once a
        file is loaded — menu items grey out until then.
        """
        # win.present — F5 toggles fullscreen present mode.
        action = Gio.SimpleAction.new("present", None)
        action.connect("activate", lambda *_: self.action_present_toggle(False))
        action.set_enabled(False)
        self.add_action(action)
        self._present_action = action

        # win.present-windowed — borderless windowed present (no shortcut
        # by default; could add Shift+F5 if it earns it).
        win_action = Gio.SimpleAction.new("present-windowed", None)
        win_action.connect("activate", lambda *_: self.action_present_toggle(True))
        win_action.set_enabled(False)
        self.add_action(win_action)
        self._present_windowed_action = win_action

        app = self.get_application()
        if app:
            app.set_accels_for_action("win.present", ["F5"])

        # Export actions: one per format.  Disabled until a file is open.
        self._export_actions: list[Gio.SimpleAction] = []
        for suffix, _label, _ext in EXPORT_FORMATS:
            act = Gio.SimpleAction.new(f"export-{suffix}", None)
            # Default arg `s=suffix` captures the current value at lambda
            # creation time, dodging the classic late-binding trap.
            act.connect("activate", lambda a, p, s=suffix: self.action_export(s))
            act.set_enabled(False)
            self.add_action(act)
            self._export_actions.append(act)

        # Escape: only meaningful while presenting.  Use a key controller
        # in the CAPTURE phase on the window so we see Escape *before*
        # the WebView (which has focus in present mode and would
        # otherwise swallow the key).  Outside present mode we don't
        # consume it, so other widgets see Escape as usual.
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctrl)

    def action_present_toggle(self, windowed: bool = False) -> None:
        """Toggle present mode; honour `windowed` only when entering."""
        if self._presenting:
            self._exit_present()
        else:
            self._enter_present(windowed=windowed)

    def _enter_present(self, windowed: bool = False) -> None:
        """Start present mode.

        Snapshots the pre-present layout and decoration state so
        _exit_present can restore exactly what was showing before.
        Hides the headerbar, forces preview-only, and either fullscreens
        or removes WM decorations depending on `windowed`.
        """
        if self.document.path is None:
            self._toast("Open a file before presenting.")
            return
        self._pre_present_layout = self._layout
        self._pre_present_decorated = self.get_decorated()
        # Force preview-only so the slide fills the visible area.
        self._btn_preview.set_active(True)
        self._toolbar_view.set_reveal_top_bars(False)
        if windowed:
            # Borderless floating window — handy for Zoom-style screen
            # shares where the user wants the slide to sit alongside
            # other windows.  Keep size/position so the user can park it.
            self.set_decorated(False)
            hint = "Press Escape to exit"
        else:
            self.fullscreen()
            hint = "Press Escape or F5 to exit"
        self._presenting = True
        # WebKit takes focus so marp's own arrow-key navigation works.
        self.preview.web_view.grab_focus()
        self._toast(hint)

    def _exit_present(self) -> None:
        """Undo whatever _enter_present did; idempotent."""
        if self.is_fullscreen():
            self.unfullscreen()
        self.set_decorated(self._pre_present_decorated)
        self._toolbar_view.set_reveal_top_bars(True)
        if self._pre_present_layout == LAYOUT_EDITOR:
            self._btn_editor.set_active(True)
        elif self._pre_present_layout == LAYOUT_SPLIT:
            self._btn_split.set_active(True)
        self._pre_present_layout = None
        self._presenting = False

    def _on_key_pressed(self, _ctrl, keyval, _keycode, _state) -> bool:
        # Only consume Escape while presenting (fullscreen OR windowed).
        # Otherwise let the focused widget handle it (close popovers etc).
        if keyval == Gdk.KEY_Escape and self._presenting:
            self._exit_present()
            return True
        return False

    def _on_fullscreen_changed(self, *_):
        # Safety net: if something else (window manager, compositor)
        # leaves fullscreen, make sure the chrome comes back too.
        if not self.is_fullscreen() and not self._toolbar_view.get_reveal_top_bars():
            self._toolbar_view.set_reveal_top_bars(True)
            self._pre_present_layout = None

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def _format_spec(self, suffix: str):
        """Look up a row of the EXPORT_FORMATS table by its action suffix."""
        for spec in EXPORT_FORMATS:
            if spec[0] == suffix:
                return spec
        raise KeyError(suffix)

    def action_export(self, suffix: str) -> None:
        """Open a save dialog for the given format, then kick off export."""
        if self.document.path is None:
            return
        _, label, ext = self._format_spec(suffix)
        dlg = Gtk.FileDialog.new()
        dlg.set_title(f"Export as {label}")
        dlg.set_initial_name(f"{self.document.path.stem}.{ext}")
        if self.document.path.parent.is_dir():
            dlg.set_initial_folder(Gio.File.new_for_path(str(self.document.path.parent)))
        dlg.save(self, None, lambda d, r: self._on_export_dest_chosen(d, r, suffix))

    def _on_export_dest_chosen(self, dlg, res, suffix: str) -> None:
        try:
            f = dlg.save_finish(res)
        except GLib.Error:
            # User dismissed the dialog; nothing to do.
            return
        if not f or self.document.path is None:
            return
        out_path = f.get_path()
        _, label, ext = self._format_spec(suffix)
        if not out_path.lower().endswith("." + ext):
            out_path += "." + ext

        # The export engines read the source file from disk, so flush any
        # unsaved edits first — otherwise the export would lag a debounce
        # cycle behind what the user sees on screen.
        if self.document.is_dirty(self.editor.get_text()):
            try:
                self.document.save(self.editor.get_text())
            except OSError as e:
                self._toast(f"Couldn't save before export: {e}")
                return

        self._toast(f"Exporting {label}…")
        export.run(suffix, str(self.document.path), out_path, self._on_export_done)

    def _on_export_done(self, ok: bool, message: str) -> bool:
        """Report an export result. Invoked on the main loop by lantern.export.

        Failure toasts are sticky so the reason doesn't vanish before the user
        reads it. Returns False so the GLib idle source that may carry this
        callback fires only once.
        """
        self._toast(message, sticky=not ok)
        return False

    def _build_welcome(self) -> Gtk.Widget:
        page = Adw.StatusPage()
        page.set_icon_name(APP_ID)
        page.set_title(APP_NAME)
        page.set_description("Author slide decks in Markdown, with live preview.")

        new_btn = Gtk.Button(label="New presentation")
        new_btn.add_css_class("pill")
        new_btn.add_css_class("suggested-action")
        new_btn.connect("clicked", lambda *_: self.action_new_file())

        open_btn = Gtk.Button(label="Open file…")
        open_btn.add_css_class("pill")
        open_btn.connect("clicked", lambda *_: self.action_open_file())

        button_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
        )
        button_box.append(new_btn)
        button_box.append(open_btn)
        page.set_child(button_box)
        return page

    def _build_doc(self) -> Gtk.Widget:
        # Paned is GTK's draggable two-pane container.  set_shrink_*=False
        # stops a pane from being squashed to zero by the handle drag;
        # set_resize_*=True spreads window resizes across both sides.
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_wide_handle(True)
        self._paned.set_shrink_start_child(False)
        self._paned.set_shrink_end_child(False)
        self._paned.set_resize_start_child(True)
        self._paned.set_resize_end_child(True)
        self._paned.set_start_child(self.editor.widget)
        self._paned.set_end_child(self.preview.widget)
        # Initial split ~50/50; user can drag the handle.
        self._paned.set_position(600)
        return self._paned

    # ------------------------------------------------------------------
    # File actions
    # ------------------------------------------------------------------
    def _markdown_filters(self) -> Gio.ListStore:
        """A single FileFilter that matches *.md and *.markdown."""
        filt = Gtk.FileFilter()
        filt.set_name("Markdown")
        for pat in ("*.md", "*.markdown"):
            filt.add_pattern(pat)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(filt)
        return store

    def _initial_folder(self) -> Gio.File | None:
        """Where to start the file dialog: last-used folder, then ~/Documents."""
        last = self._state.get("last_folder")
        if last and os.path.isdir(last):
            return Gio.File.new_for_path(last)
        default = Path.home() / "Documents"
        if default.is_dir():
            return Gio.File.new_for_path(str(default))
        return None

    def action_new_file(self) -> None:
        dlg = Gtk.FileDialog.new()
        dlg.set_title("New presentation")
        dlg.set_initial_name("presentation.md")
        dlg.set_filters(self._markdown_filters())
        folder = self._initial_folder()
        if folder:
            dlg.set_initial_folder(folder)
        dlg.save(self, None, self._on_new_chosen)

    def _on_new_chosen(self, dlg, res) -> None:
        try:
            f = dlg.save_finish(res)
        except GLib.Error:
            return
        if not f:
            return
        path = f.get_path()
        # File dialog doesn't auto-append the extension on every desktop;
        # do it ourselves so the user reliably ends up with a .md file.
        if not path.endswith((".md", ".markdown")):
            path += ".md"
        if not os.path.exists(path):
            try:
                Path(path).write_text(default_template(Path(path).stem), encoding="utf-8")
            except OSError as e:
                self._toast(f"Couldn't create file: {e}")
                return
        self.load_file(path)

    def action_open_file(self) -> None:
        dlg = Gtk.FileDialog.new()
        dlg.set_title("Open presentation")
        dlg.set_filters(self._markdown_filters())
        folder = self._initial_folder()
        if folder:
            dlg.set_initial_folder(folder)
        dlg.open(self, None, self._on_open_chosen)

    def _on_open_chosen(self, dlg, res) -> None:
        try:
            f = dlg.open_finish(res)
        except GLib.Error:
            return
        if f:
            self.load_file(f.get_path())

    def load_file(self, path: str) -> None:
        """Adopt `path` as the current document and start its preview."""
        try:
            text = self.document.load(path)
        except OSError as e:
            self._toast(f"Couldn't open: {e}")
            return
        self.editor.set_text(text)
        self._state["last_folder"] = str(self.document.path.parent)
        _save_state(self._state)
        self._start_preview(self.document.path)
        self._content_stack.set_visible_child_name("doc")
        # File-dependent actions become available now that we have a doc.
        self._present_action.set_enabled(True)
        self._present_windowed_action.set_enabled(True)
        for act in self._export_actions:
            act.set_enabled(True)

    # ------------------------------------------------------------------
    # Preview wiring
    # ------------------------------------------------------------------
    def _start_preview(self, path: Path) -> None:
        try:
            self.marp.start_for_directory(path.parent)
        except (RuntimeError, TimeoutError) as e:
            self._toast(str(e))
            self.preview.load_placeholder("Preview unavailable. Is marp installed?")
            return
        self.preview.load_url(self.marp.url_for(path))

    # ------------------------------------------------------------------
    # Editor / autosave
    # ------------------------------------------------------------------
    def _on_editor_changed(self, _editor) -> None:
        # Debounce: every keystroke cancels the previous pending save and
        # re-arms a fresh timer, so we only actually write to disk once
        # the user pauses typing for AUTOSAVE_DEBOUNCE_MS.
        if self._save_timeout:
            GLib.source_remove(self._save_timeout)
        self._save_timeout = GLib.timeout_add(AUTOSAVE_DEBOUNCE_MS, self._autosave)

    def _autosave(self) -> bool:
        # GLib timeouts fire on the main loop, so we're safe to touch
        # the buffer here.  Returning SOURCE_REMOVE makes this a one-shot;
        # the next keystroke arms a new timer via _on_editor_changed.
        self._save_timeout = 0
        if self.document.path is None:
            return GLib.SOURCE_REMOVE
        text = self.editor.get_text()
        if not self.document.is_dirty(text):
            return GLib.SOURCE_REMOVE
        try:
            self.document.save(text)
        except OSError as e:
            self._toast(f"Save failed: {e}")
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Layout toggle
    # ------------------------------------------------------------------
    def _on_layout_toggled(self, btn: Gtk.ToggleButton, mode: str) -> None:
        # The grouped toggles fire 'toggled' twice on each change — once
        # for the button going OFF and once for the one going ON.  Only
        # the latter has get_active() == True, so we filter here.
        if not btn.get_active():
            return
        self._layout = mode
        editor_visible  = mode in (LAYOUT_EDITOR, LAYOUT_SPLIT)
        preview_visible = mode in (LAYOUT_PREVIEW, LAYOUT_SPLIT)
        # Hiding a Paned child gives the visible one the full width;
        # showing both restores the split at the previous position.
        self.editor.widget.set_visible(editor_visible)
        self.preview.widget.set_visible(preview_visible)

    # ------------------------------------------------------------------
    # Title + state plumbing
    # ------------------------------------------------------------------
    def _on_path_changed(self, *_):
        self._title.set_title(self.document.title)
        self._title.set_subtitle(self.document.subtitle)
        self.set_title(f"{self.document.title} — {APP_NAME}")

    def _show_welcome(self) -> None:
        self._content_stack.set_visible_child_name("welcome")

    def _toast(self, message: str, sticky: bool = False) -> None:
        """Pop a transient banner at the bottom of the window.

        sticky=True keeps the toast until the user dismisses it — used
        for export errors so the message doesn't vanish before they
        notice.
        """
        t = Adw.Toast.new(message)
        if sticky:
            t.set_timeout(0)           # only goes away on dismiss click
            t.set_priority(Adw.ToastPriority.HIGH)
        else:
            t.set_timeout(4)
        self._toast_overlay.add_toast(t)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def _on_close_request(self, *_) -> bool:
        # Returning False lets the window close.  We use the chance to
        # flush any pending autosave (the debounce timer might not have
        # fired yet) and to shut down the marp subprocess cleanly.
        if self._save_timeout:
            GLib.source_remove(self._save_timeout)
            self._save_timeout = 0
        if self.document.path:
            text = self.editor.get_text()
            if self.document.is_dirty(text):
                try:
                    self.document.save(text)
                except OSError:
                    pass
        self.marp.stop()
        return False
