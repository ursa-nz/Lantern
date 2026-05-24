# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""Resources window — a deck's theme, images, and fonts.

A non-modal top-level window, so it floats free of the editor and stays open
while you work. Three groups:
- Theme: a base-theme picker (Marp builtins, curated themes Lantern ships, and
  the bundle's own) plus an Edit CSS action that opens the theme in the user's
  editor.
- Images: add via the + button or by dropping files; insert through the
  placement dialog (inline or background, sizing, filters); a sort control
  orders them, newest added first by default. Deleting warns when the asset is
  still referenced.
- Fonts: add likewise, then assign one to a slide role (body/headings/mono).

(GTK4/Wayland has no app-controlled always-on-top, so "floating" here means a
separate non-modal window. GNOME's title-bar "Always on Top" pins it if wanted.)

- ResourcesWindow: the window. refresh() rebuilds the lists and picker from the
  bundle's working dir; the window holds the live Document, so it always
  reflects the currently open deck.
- prompt_insert / _ImageDialog: the image placement form, shared by the Images
  Insert button and the editor's drop-target.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

import os

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from lantern import bundle

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".avif"}
FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2"}


def install_file_drop(widget, on_files, capture: bool = False) -> Gtk.DropTargetAsync:
    """Wire an async file drop-target onto `widget`.

    Calls `on_files(list_of_Gio_File)` once a drop's files have been read.

    Reads the drag's `text/uri-list` rather than letting GdkFileList pick the
    `application/vnd.portal.filetransfer` representation: under a flatpak that
    portal path fails with "Invalid parent directory" for drags from a
    non-sandboxed source, while uri-list carries real file:// paths the app can
    already read via --filesystem=home. The read is async (and the stream is
    spliced async) because a synchronous read would block the main loop
    mid-transfer and deadlock. `capture=True` runs in the capture phase so the
    editor claims image drops before GtkSourceView pastes the path in as text.
    """
    formats = Gdk.ContentFormats.new_for_gtype(Gdk.FileList)
    target = Gtk.DropTargetAsync.new(formats, Gdk.DragAction.COPY)
    if capture:
        target.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

    def _uris_done(drop_obj, res, _u):
        try:
            stream, _mime = drop_obj.read_finish(res)
        except GLib.Error:
            drop_obj.finish(Gdk.DragAction(0))
            return
        out = Gio.MemoryOutputStream.new_resizable()
        out.splice_async(
            stream,
            Gio.OutputStreamSpliceFlags.CLOSE_SOURCE | Gio.OutputStreamSpliceFlags.CLOSE_TARGET,
            GLib.PRIORITY_DEFAULT, None, _splice_done, drop_obj)

    def _splice_done(out, res, drop_obj):
        try:
            out.splice_finish(res)
        except GLib.Error:
            drop_obj.finish(Gdk.DragAction(0))
            return
        data = out.steal_as_bytes().get_data()
        files = [Gio.File.new_for_uri(line.strip())
                 for line in data.decode("utf-8", "replace").splitlines()
                 if line.strip() and not line.startswith("#")]
        drop_obj.finish(Gdk.DragAction.COPY)
        on_files(files)

    def _value_done(drop_obj, res, _u):
        try:
            value = drop_obj.read_value_finish(res)
        except GLib.Error:
            drop_obj.finish(Gdk.DragAction(0))
            return
        files = list(value.get_files()) if value is not None else []
        drop_obj.finish(Gdk.DragAction.COPY)
        on_files(files)

    def _on_drop(_t, drop_obj, _x, _y) -> bool:
        mimes = drop_obj.get_formats().get_mime_types() or []
        if "text/uri-list" in mimes:
            drop_obj.read_async(["text/uri-list"], GLib.PRIORITY_DEFAULT, None,
                                _uris_done, None)
        else:
            drop_obj.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None,
                                      _value_done, None)
        return True

    target.connect("drop", _on_drop)
    widget.add_controller(target)
    return target


class ResourcesWindow(Adw.Window):
    """Floating, non-modal manager for the open deck's images and fonts.

    on_insert(markdown): insert a reference at the editor's cursor.
    get_deck_text(): the live deck text, used to count references before delete.
    """

    def __init__(self, parent, document, on_insert, get_deck_text,
                 on_assign_font, on_pick_theme, on_edit_css,
                 on_reset_theme, on_save_preset) -> None:
        super().__init__(title="Resources", transient_for=parent, modal=False,
                         default_width=380, default_height=560)
        self._doc = document
        self._on_insert = on_insert
        self._get_deck_text = get_deck_text
        self._on_assign_font = on_assign_font
        self._on_pick_theme = on_pick_theme
        self._on_edit_css = on_edit_css
        self._on_reset_theme = on_reset_theme
        self._on_save_preset = on_save_preset
        self._rows: list = []   # every row we've added, so refresh can clear them

        # Theme: a base-theme dropdown plus Edit CSS, Reset, and Save as preset
        # actions. Selecting writes the deck's `theme:` directive (see
        # window._pick_theme). _suppress_theme stops the programmatic selection
        # refresh() makes from looping back.
        self._theme = Adw.PreferencesGroup(title="Theme")
        self._theme_descriptors: list = []
        self._suppress_theme = False
        self._theme_combo = Adw.ComboRow(title="Base theme")
        self._theme_combo.connect("notify::selected", self._on_theme_selected)
        self._theme.add(self._theme_combo)
        edit_css = Adw.ActionRow(title="Edit CSS",
                                 subtitle="Open this deck's theme in your editor",
                                 activatable=True)
        edit_css.add_suffix(Gtk.Image.new_from_icon_name("document-edit-symbolic"))
        edit_css.connect("activated", lambda _r: self._on_edit_css())
        self._theme.add(edit_css)
        # Reset is only meaningful for a theme Lantern ships; _refresh_theme
        # shows it only then.
        self._reset_row = Adw.ActionRow(title="Reset theme",
                                        subtitle="Undo your edits, back to the shipped version",
                                        activatable=True)
        self._reset_row.add_suffix(Gtk.Image.new_from_icon_name("edit-undo-symbolic"))
        self._reset_row.connect("activated", lambda _r: self._on_reset_theme())
        self._theme.add(self._reset_row)
        save_preset = Adw.ActionRow(title="Save as preset",
                                    subtitle="Reuse this theme in other decks",
                                    activatable=True)
        save_preset.add_suffix(Gtk.Image.new_from_icon_name("document-save-symbolic"))
        save_preset.connect("activated", lambda _r: self._on_save_preset())
        self._theme.add(save_preset)

        self._image_order = "recent"   # newest added first
        self._images = Adw.PreferencesGroup(title="Images")
        self._images.set_header_suffix(self._image_header_suffix())
        self._fonts = Adw.PreferencesGroup(title="Fonts")
        self._fonts.set_header_suffix(self._add_button(self._on_add_font))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        box.append(self._images)
        box.append(self._theme)
        box.append(self._fonts)
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_child(box)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(scroller)
        self.set_content(toolbar)

        # Drop files anywhere in the window to add them to the bundle.
        install_file_drop(scroller, self._handle_dropped_files)

        self.refresh()

    # ------------------------------------------------------------------
    # List building
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        for group, row in self._rows:
            group.remove(row)
        self._rows.clear()
        work_dir = self._doc.work_dir
        if work_dir is None:
            return
        self._refresh_theme()

        images = bundle.list_images(work_dir, self._image_order)
        if images:
            for rel in images:
                row = Adw.ActionRow(title=GLib.markup_escape_text(_basename(rel)))
                row.add_suffix(self._icon_button(
                    "insert-image-symbolic", "Insert into deck",
                    lambda _b, r=rel: self._insert_image(r)))
                row.add_suffix(self._icon_button(
                    "user-trash-symbolic", "Delete",
                    lambda _b, r=rel: self._delete(r), destructive=True))
                self._add_row(self._images, row)
        else:
            self._add_row(self._images, self._placeholder("Drop an image here, or use +."))

        fonts = bundle.list_fonts(work_dir)
        if fonts:
            roles_by_font: dict = {}   # rel -> [role, ...]
            for role, frel in bundle.font_roles(work_dir).items():
                roles_by_font.setdefault(frel, []).append(role)
            for rel in fonts:
                assigned = roles_by_font.get(rel, [])
                row = Adw.ActionRow(title=GLib.markup_escape_text(_basename(rel)))
                if assigned:
                    row.set_subtitle(", ".join(bundle.ROLE_LABELS[r] for r in assigned))
                row.add_suffix(self._role_button(rel, set(assigned)))
                row.add_suffix(self._icon_button(
                    "user-trash-symbolic", "Delete",
                    lambda _b, r=rel: self._delete(r), destructive=True))
                self._add_row(self._fonts, row)
        else:
            self._add_row(self._fonts, self._placeholder(
                "Drop a font here, or use +. Then pick what it styles."))

    def _add_row(self, group, row) -> None:
        group.add(row)
        self._rows.append((group, row))

    # ------------------------------------------------------------------
    # Theme picker
    # ------------------------------------------------------------------
    def _refresh_theme(self) -> None:
        wd = self._doc.work_dir
        if wd is None:
            return
        self._theme_descriptors = bundle.available_themes(wd)
        model = Gtk.StringList()
        for d in self._theme_descriptors:
            model.append(d["label"])
        current = bundle.base_theme(wd)
        index = next((i for i, d in enumerate(self._theme_descriptors)
                      if d["name"] == current), 0)
        # Setting the model resets selection to 0 and fires notify; bracket the
        # whole update so neither that nor set_selected re-triggers a pick.
        self._suppress_theme = True
        self._theme_combo.set_model(model)
        self._theme_combo.set_selected(index)
        self._suppress_theme = False
        self._reset_row.set_visible(bundle.is_curated(current))

    def _on_theme_selected(self, combo, _param) -> None:
        if self._suppress_theme:
            return
        idx = combo.get_selected()
        if 0 <= idx < len(self._theme_descriptors):
            self._on_pick_theme(self._theme_descriptors[idx])
            # The new base may change whether Reset applies, so refresh (idle,
            # never from inside the combo's own signal).
            self._schedule_refresh()

    # ------------------------------------------------------------------
    # Add / insert / delete
    # ------------------------------------------------------------------
    def _on_add_image(self, _btn) -> None:
        self._pick("Add image", IMAGE_EXTS, bundle.add_image)

    def _on_add_font(self, _btn) -> None:
        self._pick("Add font", FONT_EXTS, bundle.add_font)

    def _image_header_suffix(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(self._sort_button())
        box.append(self._add_button(self._on_add_image))
        return box

    def _sort_button(self) -> Gtk.MenuButton:
        btn = Gtk.MenuButton(icon_name="view-sort-descending-symbolic",
                             valign=Gtk.Align.CENTER, tooltip_text="Sort images")
        btn.add_css_class("flat")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                        margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        group = None
        for value, label in (("recent", "Newest first"),
                             ("oldest", "Oldest first"),
                             ("name", "Name")):
            radio = Gtk.CheckButton(label=label)
            if group is None:
                group = radio
            else:
                radio.set_group(group)
            radio.set_active(value == self._image_order)
            radio.connect("toggled", self._on_sort_toggled, value)
            inner.append(radio)
        popover = Gtk.Popover()
        popover.set_child(inner)
        btn.set_popover(popover)
        return btn

    def _on_sort_toggled(self, radio, value) -> None:
        # Grouped radios fire for both the off and on transitions; act on on.
        if radio.get_active():
            self._image_order = value
            self._schedule_refresh()

    def _pick(self, title, exts, adder) -> None:
        if self._doc.work_dir is None:
            return
        dlg = Gtk.FileDialog.new()
        dlg.set_title(title)
        dlg.set_filters(_filters(title, exts))
        dlg.open(self, None, lambda d, r: self._on_picked(d, r, adder))

    def _on_picked(self, dlg, res, adder) -> None:
        try:
            f = dlg.open_finish(res)
        except GLib.Error:
            return
        if f and f.get_path():
            adder(self._doc.work_dir, f.get_path())
            self.refresh()

    def _insert_image(self, rel) -> None:
        prompt_insert(self, rel, self._on_insert)

    def _delete(self, rel) -> None:
        uses = bundle.count_references(self._get_deck_text(), rel)
        if uses == 0:
            self._do_delete(rel)
            return
        dlg = Adw.AlertDialog(
            heading="Delete an asset that's in use?",
            body=f"{_basename(rel)} is referenced {uses} time(s) in the deck. "
                 "Deleting it will break those references.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, resp, r=rel: self._do_delete(r) if resp == "delete" else None)
        dlg.present(self)

    def _do_delete(self, rel) -> None:
        work_dir = self._doc.work_dir
        # Note the roles this font filled *before* removing it (font_roles drops
        # missing files, so reading it after the delete would lose them).
        affected = []
        if work_dir is not None:
            affected = [role for role, frel in bundle.font_roles(work_dir).items()
                        if frel == rel]
        bundle.delete_asset(self._doc.work_dir, rel)
        # Clear each role so the theme reverts to the default font and the
        # re-save drops the now-removed file cleanly (no dangling reference).
        for role in affected:
            self._on_assign_font(role, None)
        self.refresh()

    # ------------------------------------------------------------------
    # Typography — assign a font to a slide role (body / headings / mono)
    # ------------------------------------------------------------------
    def _role_button(self, rel, assigned: set) -> Gtk.MenuButton:
        btn = Gtk.MenuButton(label="Use for", valign=Gtk.Align.CENTER,
                             tooltip_text="Use this font for part of the deck")
        btn.add_css_class("flat")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        for role, label in bundle.ROLE_LABELS.items():
            check = Gtk.CheckButton(label=label)
            check.set_active(role in assigned)
            check.connect("toggled", self._on_role_toggled, role, rel)
            box.append(check)
        popover = Gtk.Popover()
        popover.set_child(box)
        popover.connect("closed", lambda _p: self._schedule_refresh())
        btn.set_popover(popover)
        return btn

    def _on_role_toggled(self, check, role, rel) -> None:
        self._on_assign_font(role, rel if check.get_active() else None)

    def _schedule_refresh(self) -> None:
        # Defer: never rebuild the list from inside a child widget's own signal.
        GLib.idle_add(self._refresh_once)

    def _refresh_once(self) -> bool:
        self.refresh()
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Drop
    # ------------------------------------------------------------------
    def _handle_dropped_files(self, files) -> None:
        work_dir = self._doc.work_dir
        if work_dir is None:
            return
        added = False
        for gf in files:
            path = gf.get_path()
            if not path:
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext in IMAGE_EXTS:
                bundle.add_image(work_dir, path)
                added = True
            elif ext in FONT_EXTS:
                bundle.add_font(work_dir, path)
                added = True
        if added:
            self.refresh()

    # ------------------------------------------------------------------
    # Small widget helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _add_button(callback) -> Gtk.Button:
        btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        btn.connect("clicked", callback)
        return btn

    @staticmethod
    def _icon_button(icon, tooltip, callback, destructive=False) -> Gtk.Button:
        btn = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=tooltip)
        btn.add_css_class("flat")
        if destructive:
            btn.add_css_class("error")
        btn.connect("clicked", callback)
        return btn

    @staticmethod
    def _placeholder(text) -> Adw.ActionRow:
        row = Adw.ActionRow(title=text)
        row.set_sensitive(False)
        return row


def prompt_insert(parent, rel, on_insert, on_done=None) -> None:
    """Open the image-insert dialog, then hand the markdown to `on_insert`.

    Shared by the Resources list's Insert button and the editor's drop-target.
    `on_done`, if given, is called when the dialog closes (whether the user
    inserted or cancelled), so a caller can chain several drops one at a time
    instead of stacking a dialog per file.
    """
    dlg = _ImageDialog(rel, on_insert)
    if on_done is not None:
        dlg.connect("closed", lambda _d: on_done())
    dlg.present(parent)


class _ImageDialog(Adw.Dialog):
    """Form for placing an image: inline (with w:/h:) or as a background
    (position, split, fit), with optional filters. Builds a Marp directive via
    bundle.image_markdown and hands it to on_insert."""

    def __init__(self, rel, on_insert) -> None:
        super().__init__(title="Insert image")
        self._rel = rel
        self._on_insert = on_insert
        self.set_content_width(440)

        insert = Gtk.Button(label="Insert")
        insert.add_css_class("suggested-action")
        insert.connect("clicked", self._on_insert_clicked)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header = Adw.HeaderBar(show_start_title_buttons=False,
                               show_end_title_buttons=False)
        header.pack_start(cancel)
        header.pack_end(insert)

        page = Adw.PreferencesPage()

        placement_group = Adw.PreferencesGroup(description=_basename(rel))
        self._placement = Adw.ComboRow(
            title="Placement", model=Gtk.StringList.new(["Inline", "Background"]))
        self._placement.connect("notify::selected", self._sync_visibility)
        placement_group.add(self._placement)
        page.add(placement_group)

        self._size_group = Adw.PreferencesGroup(title="Size")
        self._width = Adw.EntryRow(title="Width (e.g. 300 or 50%)")
        self._height = Adw.EntryRow(title="Height (optional)")
        self._size_group.add(self._width)
        self._size_group.add(self._height)
        page.add(self._size_group)

        self._bg_group = Adw.PreferencesGroup(title="Background")
        self._bg_position = Adw.ComboRow(
            title="Position", model=Gtk.StringList.new(["Full", "Left", "Right"]))
        self._bg_position.connect("notify::selected", self._sync_visibility)
        self._bg_split = Adw.SpinRow.new_with_range(10, 90, 5)
        self._bg_split.set_title("Split %")
        self._bg_split.set_value(50)
        self._bg_size = Adw.ComboRow(
            title="Size", model=Gtk.StringList.new(["Default", "Cover", "Fit", "Auto"]))
        self._bg_group.add(self._bg_position)
        self._bg_group.add(self._bg_split)
        self._bg_group.add(self._bg_size)
        page.add(self._bg_group)

        filter_group = Adw.PreferencesGroup(title="Filters")
        self._filters = {}
        for key in bundle.IMAGE_FILTERS:
            row = Adw.SwitchRow(title=key.capitalize())
            self._filters[key] = row
            filter_group.add(row)
        page.add(filter_group)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(page)
        self.set_child(toolbar)
        self._sync_visibility()

    def _sync_visibility(self, *_) -> None:
        background = self._placement.get_selected() == 1
        self._size_group.set_visible(not background)
        self._bg_group.set_visible(background)
        # The split percentage only matters for a left/right background.
        self._bg_split.set_visible(self._bg_position.get_selected() in (1, 2))

    def _on_insert_clicked(self, _btn) -> None:
        background = self._placement.get_selected() == 1
        position = bundle.BG_POSITIONS[self._bg_position.get_selected()]
        size = ("default",) + bundle.BG_SIZES
        markdown = bundle.image_markdown(
            self._rel,
            background=background,
            width=self._width.get_text(),
            height=self._height.get_text(),
            bg_position=position,
            bg_split_pct=(int(self._bg_split.get_value())
                          if position in ("left", "right") else None),
            bg_size=size[self._bg_size.get_selected()],
            filters=[k for k, row in self._filters.items() if row.get_active()])
        self._on_insert(markdown)
        self.close()


def _basename(rel) -> str:
    return rel.rsplit("/", 1)[-1]


def _filters(name, exts) -> Gio.ListStore:
    filt = Gtk.FileFilter()
    filt.set_name(name)
    for ext in exts:
        filt.add_pattern("*" + ext)
    store = Gio.ListStore.new(Gtk.FileFilter)
    store.append(filt)
    return store
