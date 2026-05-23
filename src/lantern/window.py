# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""Main window: header, layout toggle, present modes, export, autosave.

- LanternWindow: the Adw.ApplicationWindow.  Owns the Document, the
  MarpServer, the Editor and Preview, and wires the three together.
- action_new_file / action_open_file / action_save / open_path: the
  .lantern lifecycle.  Opening a bundle unpacks it to a working dir;
  opening a loose .md imports it; Save re-zips the working dir.
- action_export: opens a save dialog, then hands off to lantern.export to
  produce HTML (marp), PDF (WebKit print), or PPTX (pandoc).
- action_present_toggle: enters/exits present mode.  windowed=True gives
  a borderless floating window (Zoom-friendly); windowed=False fullscreens.
- _autosave: debounced write-back to the working dir's deck.md so marp's
  live-reload picks changes up; Save is what re-zips the bundle.

State persisted to ~/.config/lantern/state.json across runs: the last-used
folder for file dialogs, the recent-files list, and a default author name.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

import json
import os
from datetime import date
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from lantern import APP_ID, APP_NAME, bundle, export, resources
from lantern.document import Document
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
        self.preview = Preview(on_reload=self._refresh_preview)

        self._state = _load_state()
        self._save_timeout = 0
        self._layout = LAYOUT_SPLIT
        self._resources_win = None
        # "Edit CSS" watches the theme file so an external editor's save
        # reloads the preview; the timeout coalesces a burst of write events.
        self._css_monitor = None
        self._css_reload_timeout = 0
        # Debounce for syncing the preview to the slide under the cursor.
        self._slide_sync_timeout = 0

        self._build_ui()
        self._install_shortcuts()
        self.editor.connect("changed", self._on_editor_changed)
        self.editor.connect("cursor-moved", self._on_cursor_moved)
        self._install_editor_drop()
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
        # Resources: a toggle that shows the floating images/fonts window.
        self._resources_btn = Gtk.ToggleButton(icon_name="image-x-generic-symbolic")
        self._resources_btn.set_tooltip_text("Theme, images, and fonts")
        self._resources_btn.set_sensitive(False)
        self._resources_btn.connect("toggled", self._on_resources_toggled)
        self._header.pack_end(self._resources_btn)

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
        file_section.append("Save",      "win.save")
        file_section.append("Properties…", "win.properties")
        menu.append_section(None, file_section)
        view_section = Gio.Menu()
        view_section.append("Reload preview", "win.reload-preview")
        export_menu = Gio.Menu()
        for suffix, label, _ext in EXPORT_FORMATS:
            export_menu.append(label, f"win.export-{suffix}")
        view_section.append_submenu("Export…", export_menu)
        menu.append_section(None, view_section)
        meta_section = Gio.Menu()
        meta_section.append("Preferences", "win.preferences")
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

        Save, present and export actions start disabled and are enabled
        once a deck is loaded — menu items grey out until then.
        """
        app = self.get_application()

        # win.save — Ctrl+S re-zips the working dir into the .lantern.
        save_action = Gio.SimpleAction.new("save", None)
        save_action.connect("activate", lambda *_: self.action_save())
        save_action.set_enabled(False)
        self.add_action(save_action)
        self._save_action = save_action
        if app:
            app.set_accels_for_action("win.save", ["<primary>s"])

        # win.properties — edit the bundle's title/author (lantern.json).
        prop_action = Gio.SimpleAction.new("properties", None)
        prop_action.connect("activate", lambda *_: self._show_properties())
        prop_action.set_enabled(False)
        self.add_action(prop_action)
        self._properties_action = prop_action

        # win.preferences — app settings (the default author name).
        prefs_action = Gio.SimpleAction.new("preferences", None)
        prefs_action.connect("activate", lambda *_: self._show_preferences())
        self.add_action(prefs_action)

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

        if app:
            app.set_accels_for_action("win.present", ["F5"])

        # Export actions: one per format.  Disabled until a deck is open.
        self._export_actions: list[Gio.SimpleAction] = []
        for suffix, _label, _ext in EXPORT_FORMATS:
            act = Gio.SimpleAction.new(f"export-{suffix}", None)
            # Default arg `s=suffix` captures the current value at lambda
            # creation time, dodging the classic late-binding trap.
            act.connect("activate", lambda a, p, s=suffix: self.action_export(s))
            act.set_enabled(False)
            self.add_action(act)
            self._export_actions.append(act)

        # win.reload-preview — rebuild the preview. Also offered on the
        # preview's own error page. Disabled until a deck is open.
        reload_action = Gio.SimpleAction.new("reload-preview", None)
        reload_action.connect("activate", lambda *_: self._refresh_preview())
        reload_action.set_enabled(False)
        self.add_action(reload_action)
        self._reload_preview_action = reload_action

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
        if self.document.deck_path is None:
            self._toast("Open a deck before presenting.")
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
        if self.document.deck_path is None:
            return
        _, label, ext = self._format_spec(suffix)
        dlg = Gtk.FileDialog.new()
        dlg.set_title(f"Export as {label}")
        dlg.set_initial_name(f"{self.document.title}.{ext}")
        folder = self._save_folder()
        if folder:
            dlg.set_initial_folder(folder)
        dlg.save(self, None, lambda d, r: self._on_export_dest_chosen(d, r, suffix))

    def _on_export_dest_chosen(self, dlg, res, suffix: str) -> None:
        try:
            f = dlg.save_finish(res)
        except GLib.Error:
            # User dismissed the dialog; nothing to do.
            return
        if not f or self.document.deck_path is None:
            return
        out_path = f.get_path()
        _, label, ext = self._format_spec(suffix)
        if not out_path.lower().endswith("." + ext):
            out_path += "." + ext

        # The export engines read deck.md from the working dir, so flush the
        # latest editor text there first — otherwise the export would lag a
        # debounce cycle behind what the user sees on screen.
        try:
            self.document.write_working(self.editor.get_text())
        except OSError as e:
            self._toast(f"Couldn't save before exporting. {e}", sticky=True)
            return

        self._toast(f"Exporting {label}…")
        export.run(suffix, str(self.document.deck_path), out_path, self._on_export_done)

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

        open_btn = Gtk.Button(label="Open…")
        open_btn.add_css_class("pill")
        open_btn.connect("clicked", lambda *_: self.action_open_file())

        button_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
        )
        button_box.append(new_btn)
        button_box.append(open_btn)

        # Recent decks, filled in by _refresh_recents() from saved state.
        self._recent_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                        halign=Gtk.Align.CENTER)
        self._recent_list.add_css_class("boxed-list")
        self._recent_list.set_size_request(380, -1)
        self._recent_list.connect("row-activated", self._on_recent_activated)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                      halign=Gtk.Align.CENTER)
        box.append(button_box)
        box.append(self._recent_list)
        page.set_child(box)
        self._refresh_recents()
        return page

    def _refresh_recents(self) -> None:
        child = self._recent_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._recent_list.remove(child)
            child = nxt
        paths = [r for r in self._state.get("recent", []) if os.path.isfile(r)]
        self._recent_list.set_visible(bool(paths))
        home = str(Path.home())
        for p in paths:
            parent = str(Path(p).parent)
            subtitle = "~" + parent[len(home):] if parent.startswith(home) else parent
            row = Adw.ActionRow(title=bundle.display_name(p), subtitle=subtitle,
                                activatable=True)
            row.add_prefix(Gtk.Image.new_from_icon_name("x-office-presentation-symbolic"))
            row._lantern_path = p
            self._recent_list.append(row)

    def _on_recent_activated(self, _listbox, row) -> None:
        self.open_path(row._lantern_path)

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
    def _bundle_filter(self) -> Gio.ListStore:
        """A FileFilter matching *.lantern (for New / Save As)."""
        filt = Gtk.FileFilter()
        filt.set_name("Lantern presentation")
        filt.add_pattern("*.lantern")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(filt)
        return store

    def _open_filters(self) -> Gio.ListStore:
        """Filters for Open: Lantern bundles, plus loose Markdown to import."""
        store = Gio.ListStore.new(Gtk.FileFilter)
        bundle_f = Gtk.FileFilter()
        bundle_f.set_name("Lantern presentation")
        bundle_f.add_pattern("*.lantern")
        md_f = Gtk.FileFilter()
        md_f.set_name("Markdown (import)")
        md_f.add_pattern("*.md")
        md_f.add_pattern("*.markdown")
        store.append(bundle_f)
        store.append(md_f)
        return store

    def _initial_folder(self) -> Gio.File | None:
        """Where to start a fresh dialog: last-used folder, then ~/Documents."""
        last = self._state.get("last_folder")
        if last and os.path.isdir(last):
            return Gio.File.new_for_path(last)
        default = Path.home() / "Documents"
        if default.is_dir():
            return Gio.File.new_for_path(str(default))
        return None

    def _save_folder(self) -> Gio.File | None:
        """Folder to default save/export dialogs to: the bundle's dir if saved."""
        if self.document.bundle_path and self.document.bundle_path.parent.is_dir():
            return Gio.File.new_for_path(str(self.document.bundle_path.parent))
        return self._initial_folder()

    def action_new_file(self) -> None:
        dlg = Gtk.FileDialog.new()
        dlg.set_title("New presentation")
        dlg.set_initial_name("presentation.lantern")
        dlg.set_filters(self._bundle_filter())
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
        if not path.endswith(bundle.SUFFIX):
            path += bundle.SUFFIX
        text = self.document.new(bundle.display_name(path))
        self._stamp_meta(new=True)
        try:
            self.document.save(text, path=path)
        except OSError as e:
            self._toast(f"Couldn't create the deck. {e}", sticky=True)
            return
        self._remember_folder(path)
        self._adopt_view(text)

    def action_open_file(self) -> None:
        dlg = Gtk.FileDialog.new()
        dlg.set_title("Open presentation")
        dlg.set_filters(self._open_filters())
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
            self.open_path(f.get_path())

    def open_path(self, path: str) -> None:
        """Open a .lantern bundle (unpack) or import a loose .md."""
        p = str(path)
        try:
            if p.endswith(bundle.SUFFIX):
                text = self.document.open_bundle(p)
            else:
                # Anything else is treated as Markdown to import.
                text = self.document.import_md(p)
        except (OSError, ValueError, UnicodeDecodeError) as e:
            self._toast(f"Couldn't open that. {e}", sticky=True)
            return
        if self.document.bundle_path:
            self._remember_folder(self.document.bundle_path)
        self._adopt_view(text)

    def action_save(self) -> None:
        """Save: re-zip into the current bundle, or prompt for one (Save As)."""
        if self.document.deck_path is None:
            return
        if self.document.is_saved:
            self._stamp_meta()
            try:
                self.document.save(self.editor.get_text())
                self._toast("Saved")
            except OSError as e:
                self._toast(f"Couldn't save. {e}", sticky=True)
        else:
            self._save_as()

    def _save_as(self) -> None:
        dlg = Gtk.FileDialog.new()
        dlg.set_title("Save presentation")
        dlg.set_initial_name(f"{self.document.title}{bundle.SUFFIX}")
        dlg.set_filters(self._bundle_filter())
        folder = self._save_folder()
        if folder:
            dlg.set_initial_folder(folder)
        dlg.save(self, None, self._on_save_as_chosen)

    def _on_save_as_chosen(self, dlg, res) -> None:
        try:
            f = dlg.save_finish(res)
        except GLib.Error:
            return
        if not f:
            return
        path = f.get_path()
        if not path.endswith(bundle.SUFFIX):
            path += bundle.SUFFIX
        self._stamp_meta()
        try:
            self.document.save(self.editor.get_text(), path=path)
            self._toast("Saved")
            self._remember_folder(path)
        except OSError as e:
            self._toast(f"Couldn't save. {e}", sticky=True)

    def _adopt_view(self, text: str) -> None:
        """Show the just-loaded deck: editor text, preview, enable actions."""
        self.editor.set_text(text)
        # Show the doc page before pointing the preview at the server. WebKit
        # can silently drop a load into an unmapped view, which leaves the
        # placeholder up (the stuck preview the reload button also rescues), so
        # the WebView needs to be on screen first.
        self._content_stack.set_visible_child_name("doc")
        self._start_preview(self.document.deck_path)
        self._save_action.set_enabled(True)
        self._properties_action.set_enabled(True)
        self._present_action.set_enabled(True)
        self._present_windowed_action.set_enabled(True)
        self._resources_btn.set_sensitive(True)
        self._reload_preview_action.set_enabled(True)
        for act in self._export_actions:
            act.set_enabled(True)
        if self._resources_win is not None:
            self._resources_win.refresh()

    def _remember_folder(self, path) -> None:
        p = str(Path(path).resolve())
        self._state["last_folder"] = str(Path(p).parent)
        recent = [r for r in self._state.get("recent", []) if r != p]
        recent.insert(0, p)
        self._state["recent"] = recent[:8]
        _save_state(self._state)

    # ------------------------------------------------------------------
    # Metadata + preferences
    # ------------------------------------------------------------------
    def _stamp_meta(self, new: bool = False) -> None:
        # Update the bundle's lantern.json just before a save: a modified date
        # (and last-modified-by) always, plus created + a default author the
        # first time the deck is saved.
        wd = self.document.work_dir
        if wd is None:
            return
        today = date.today().isoformat()
        meta = bundle.read_meta(wd)
        meta["modified"] = today
        author = self._state.get("author", "").strip()
        if author:
            meta["lastModifiedBy"] = author
        if new:
            meta.setdefault("created", today)
            if author:
                meta.setdefault("author", author)
        bundle.write_meta(wd, meta)

    def _show_preferences(self) -> None:
        dlg = Adw.PreferencesDialog()
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Author",
            description="Stamped as the last-modified-by when you save a deck.")
        self._author_row = Adw.EntryRow(title="Your name")
        self._author_row.set_text(self._state.get("author", ""))
        group.add(self._author_row)
        page.add(group)

        preview = Adw.PreferencesGroup(title="Preview")
        self._sync_row = Adw.SwitchRow(
            title="Follow the cursor",
            subtitle="Show the slide the cursor is on as you edit")
        self._sync_row.set_active(self._state.get("sync_slide", True))
        preview.add(self._sync_row)
        page.add(preview)

        dlg.add(page)
        dlg.connect("closed", self._on_preferences_closed)
        dlg.present(self)

    def _on_preferences_closed(self, _dlg) -> None:
        self._state["author"] = self._author_row.get_text().strip()
        self._state["sync_slide"] = self._sync_row.get_active()
        _save_state(self._state)

    def _show_properties(self) -> None:
        if self.document.work_dir is None:
            return
        meta = bundle.read_meta(self.document.work_dir)
        dlg = Adw.PreferencesDialog(title="Properties")
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        self._prop_title = Adw.EntryRow(title="Title")
        self._prop_title.set_text(meta.get("title", ""))
        self._prop_author = Adw.EntryRow(title="Author")
        self._prop_author.set_text(meta.get("author", self._state.get("author", "")))
        group.add(self._prop_title)
        group.add(self._prop_author)
        if meta.get("modified") or meta.get("lastModifiedBy"):
            when, who = meta.get("modified", ""), meta.get("lastModifiedBy", "")
            sub = f"{when} by {who}" if when and who else (when or f"by {who}")
            group.add(Adw.ActionRow(title="Last modified", subtitle=sub, sensitive=False))
        page.add(group)
        dlg.add(page)
        dlg.connect("closed", self._on_properties_closed)
        dlg.present(self)

    def _on_properties_closed(self, _dlg) -> None:
        wd = self.document.work_dir
        if wd is None:
            return
        meta = bundle.read_meta(wd)
        for key, row in (("title", self._prop_title), ("author", self._prop_author)):
            value = row.get_text().strip()
            if value:
                meta[key] = value
            else:
                meta.pop(key, None)
        bundle.write_meta(wd, meta)
        # Persist into the .lantern if it has one (this also stamps modified).
        if self.document.is_saved:
            self.action_save()

    # ------------------------------------------------------------------
    # Resources (images & fonts)
    # ------------------------------------------------------------------
    def _on_resources_toggled(self, btn: Gtk.ToggleButton) -> None:
        if btn.get_active():
            self._resources_win = resources.ResourcesWindow(
                self, self.document, self.editor.insert_at_cursor, self.editor.get_text,
                self._assign_font, self._pick_theme, self._edit_css)
            self._resources_win.connect("close-request", self._on_resources_close)
            self._resources_win.present()
        elif self._resources_win is not None:
            win, self._resources_win = self._resources_win, None
            win.destroy()

    def _on_resources_close(self, _win) -> bool:
        # User closed the floating window directly; un-press the toggle.
        self._resources_win = None
        self._resources_btn.set_active(False)
        return False

    def _assign_font(self, role, rel) -> None:
        """Assign (or clear) a bundled font for a slide role, then reconcile.

        Regenerating the Lantern theme CSS happens in bundle.set_font_role;
        _reconcile_theme then points the deck at the right theme and reloads.
        """
        wd = self.document.work_dir
        if wd is None:
            return
        bundle.set_font_role(wd, role, rel)
        self._reconcile_theme()

    def _pick_theme(self, descriptor: dict) -> None:
        """Apply a base theme chosen in the Resources picker.

        A curated theme is copied into the bundle's styles/ first so the deck
        stays portable after a hand-unzip; builtins and the bundle's own themes
        apply by name. Then record the base and reconcile the deck's directive.
        """
        wd = self.document.work_dir
        if wd is None:
            return
        name = descriptor.get("name", "default")
        if descriptor.get("kind") == "curated":
            installed = bundle.install_curated_theme(wd, name)
            if installed is None:
                self._toast(f"Couldn't load the {name} theme.")
                return
            name = installed
        bundle.set_base_theme(wd, name)
        self._reconcile_theme()

    def _reconcile_theme(self) -> None:
        """Point the deck's `theme:` directive at the effective theme (the
        managed lantern theme when fonts are assigned, else the base theme),
        flush deck.md, persist if the deck is saved, and reload the preview.
        """
        wd = self.document.work_dir
        if wd is None:
            return
        text = self.editor.get_text()
        themed = bundle.set_theme_directive(text, bundle.effective_theme(wd))
        if themed != text:
            self.editor.set_text(themed)
        try:
            self.document.write_working(self.editor.get_text())
        except OSError as e:
            self._toast(f"Couldn't update the deck. {e}")
            return
        if self.document.is_saved:
            self.action_save()
        self.preview.reload()

    def _edit_css(self) -> None:
        """Open the deck's theme CSS in the user's editor.

        Curated/custom bases have a file in styles/ to open directly. A builtin
        base has none, so fork it into an editable custom theme that imports the
        builtin, switch the deck to it, and open that. A file monitor reloads
        the preview when the editor saves.
        """
        wd = self.document.work_dir
        if wd is None:
            return
        base = bundle.base_theme(wd)
        path = bundle.theme_css_path(wd, base)
        if path is None:
            path = bundle.ensure_custom_theme(wd, import_base=base)
            bundle.set_base_theme(wd, bundle.CUSTOM_THEME_NAME)
            self._reconcile_theme()
            if self._resources_win is not None:
                self._resources_win.refresh()
        self._watch_css(path)
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(path)))
        launcher.launch(self, None, self._on_css_launched)

    def _on_css_launched(self, launcher, result) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error:
            self._toast("Couldn't open an editor for the theme CSS.", sticky=True)

    def _watch_css(self, path) -> None:
        """Watch `path` so an external editor's save reloads the preview."""
        if self._css_monitor is not None:
            self._css_monitor.cancel()
        gfile = Gio.File.new_for_path(str(path))
        self._css_monitor = gfile.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self._css_monitor.connect("changed", self._on_css_changed)

    def _on_css_changed(self, _monitor, _f, _other, event) -> None:
        # Many editors emit a burst (or an atomic rename) per save, so coalesce
        # to a single reload a short moment after the last event.
        reload_events = (
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.RENAMED,
            Gio.FileMonitorEvent.MOVED_IN,
        )
        if event not in reload_events:
            return
        if self._css_reload_timeout:
            GLib.source_remove(self._css_reload_timeout)
        self._css_reload_timeout = GLib.timeout_add(250, self._css_reload_fire)

    def _css_reload_fire(self) -> bool:
        self._css_reload_timeout = 0
        self.preview.reload()
        return GLib.SOURCE_REMOVE

    def _install_editor_drop(self) -> None:
        # Drop an image onto the editor: bundle it, then ask inline vs
        # background and insert the reference at the cursor. capture=True so this
        # claims image drops before GtkSourceView pastes the path in as text.
        resources.install_file_drop(self.editor.widget, self._handle_editor_files,
                                     capture=True)

    def _handle_editor_files(self, files) -> None:
        if self.document.work_dir is None:
            return
        added = False
        for gf in files:
            path = gf.get_path()
            if not path or os.path.splitext(path)[1].lower() not in resources.IMAGE_EXTS:
                continue
            rel = bundle.add_image(self.document.work_dir, path)
            resources.prompt_insert(self, rel, self.editor.insert_at_cursor)
            added = True
        if added and self._resources_win is not None:
            self._resources_win.refresh()

    # ------------------------------------------------------------------
    # Preview wiring
    # ------------------------------------------------------------------
    def _refresh_preview(self) -> None:
        """Re-establish the preview when it hasn't come up on its own.

        Flush the latest text, then stop and restart the marp server and
        reload. This clears a stuck "Preview will appear once a file is open."
        when the server wedged or a first render was missed.
        """
        if self.document.deck_path is None:
            return
        try:
            self.document.write_working(self.editor.get_text())
        except OSError:
            pass
        self.marp.stop()
        self._start_preview(self.document.deck_path)

    def _start_preview(self, deck_path: Path) -> None:
        # marp --server watches the deck's directory; for a bundle that's the
        # small working dir, so the server binds quickly.
        try:
            self.marp.start_for_directory(deck_path.parent)
        except (RuntimeError, TimeoutError) as e:
            self.preview.show_error(str(e))
            return
        self.preview.load_url(self.marp.url_for(deck_path))

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
        # Writes the editor text to the working dir's deck.md (cheap), which
        # is what drives marp's live reload.  The .lantern is only updated
        # by an explicit Save.  One-shot: the next keystroke re-arms the timer.
        self._save_timeout = 0
        if self.document.deck_path is None:
            return GLib.SOURCE_REMOVE
        try:
            self.document.write_working(self.editor.get_text())
        except OSError as e:
            self._toast(f"Autosave failed. {e}")
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Slide sync — follow the cursor in the preview
    # ------------------------------------------------------------------
    def _on_cursor_moved(self, _editor) -> None:
        # Opt-out via Preferences. Debounced so dragging the caret doesn't
        # fire a navigation per line.
        if not self._state.get("sync_slide", True):
            return
        if self._slide_sync_timeout:
            GLib.source_remove(self._slide_sync_timeout)
        self._slide_sync_timeout = GLib.timeout_add(120, self._sync_slide_fire)

    def _sync_slide_fire(self) -> bool:
        self._slide_sync_timeout = 0
        if self.document.deck_path is None:
            return GLib.SOURCE_REMOVE
        idx = bundle.slide_index_at_line(self.editor.get_text(),
                                         self.editor.get_cursor_line())
        self.preview.goto_slide(idx)
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
        self.set_title(self.document.title)

    def _show_welcome(self) -> None:
        self._refresh_recents()
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
        # Returning False lets the window close.  Flush the working copy and,
        # if the deck has a bundle on disk with unsaved changes, re-zip it so
        # nothing is lost.  A never-saved deck has no bundle to write to, so
        # its working dir is just dropped.  Then stop marp and clean up.
        if self._save_timeout:
            GLib.source_remove(self._save_timeout)
            self._save_timeout = 0
        text = self.editor.get_text()
        try:
            self.document.write_working(text)
            if self.document.is_saved and self.document.is_dirty(text):
                self.document.save(text)
        except OSError:
            pass
        if self._css_reload_timeout:
            GLib.source_remove(self._css_reload_timeout)
            self._css_reload_timeout = 0
        if self._slide_sync_timeout:
            GLib.source_remove(self._slide_sync_timeout)
            self._slide_sync_timeout = 0
        if self._css_monitor is not None:
            self._css_monitor.cancel()
            self._css_monitor = None
        if self._resources_win is not None:
            self._resources_win.destroy()
        self.marp.stop()
        self.document.close()
        return False
