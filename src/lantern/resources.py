# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""Resources window — manage a deck's bundled images and fonts.

A non-modal top-level window, so it floats free of the editor and stays open
while you work. Two lists, Images and Fonts: add via the + buttons or by
dropping files onto the window; each image inserts into the deck as an inline
figure or a full-bleed background; deleting warns when the asset is still
referenced.

(GTK4/Wayland has no app-controlled always-on-top, so "floating" here means a
separate non-modal window. GNOME's title-bar "Always on Top" pins it if wanted.)

- ResourcesWindow: the window. refresh() rebuilds both lists from the bundle's
  working dir; the window holds the live Document, so it always reflects the
  currently open deck.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

import os

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from lantern import bundle

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".avif"}
FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2"}


class ResourcesWindow(Adw.Window):
    """Floating, non-modal manager for the open deck's images and fonts.

    on_insert(markdown): insert a reference at the editor's cursor.
    get_deck_text(): the live deck text, used to count references before delete.
    """

    def __init__(self, parent, document, on_insert, get_deck_text) -> None:
        super().__init__(title="Resources", transient_for=parent, modal=False,
                         default_width=380, default_height=560)
        self._doc = document
        self._on_insert = on_insert
        self._get_deck_text = get_deck_text
        self._rows: list = []   # every row we've added, so refresh can clear them

        self._images = Adw.PreferencesGroup(title="Images")
        self._images.set_header_suffix(self._add_button(self._on_add_image))
        self._fonts = Adw.PreferencesGroup(title="Fonts")
        self._fonts.set_header_suffix(self._add_button(self._on_add_font))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        box.append(self._images)
        box.append(self._fonts)
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_child(box)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(scroller)
        self.set_content(toolbar)

        # Drop files anywhere in the window to add them to the bundle.
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        scroller.add_controller(drop)

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

        images = bundle.list_images(work_dir)
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
            for rel in fonts:
                row = Adw.ActionRow(title=GLib.markup_escape_text(_basename(rel)))
                row.add_suffix(self._icon_button(
                    "user-trash-symbolic", "Delete",
                    lambda _b, r=rel: self._delete(r), destructive=True))
                self._add_row(self._fonts, row)
        else:
            self._add_row(self._fonts, self._placeholder(
                "Fonts here are referenced from a custom theme's CSS."))

    def _add_row(self, group, row) -> None:
        group.add(row)
        self._rows.append((group, row))

    # ------------------------------------------------------------------
    # Add / insert / delete
    # ------------------------------------------------------------------
    def _on_add_image(self, _btn) -> None:
        self._pick("Add image", IMAGE_EXTS, bundle.add_image)

    def _on_add_font(self, _btn) -> None:
        self._pick("Add font", FONT_EXTS, bundle.add_font)

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
            body=f"{_basename(rel)} is referenced {uses} time(s) in the deck; "
                 "deleting it will break those references.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, resp, r=rel: self._do_delete(r) if resp == "delete" else None)
        dlg.present(self)

    def _do_delete(self, rel) -> None:
        bundle.delete_asset(self._doc.work_dir, rel)
        self.refresh()

    # ------------------------------------------------------------------
    # Drop
    # ------------------------------------------------------------------
    def _on_drop(self, _target, value, _x, _y) -> bool:
        work_dir = self._doc.work_dir
        if work_dir is None:
            return False
        added = False
        for gf in value.get_files():
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
        return added

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


def prompt_insert(parent, rel, on_insert) -> None:
    """Ask inline vs background, then hand the markdown to `on_insert`.

    Shared by the Resources list's Insert button and the editor's drop-target.
    """
    dlg = Adw.AlertDialog(heading="Insert image", body=_basename(rel))
    dlg.add_response("cancel", "Cancel")
    dlg.add_response("inline", "Inline")
    dlg.add_response("bg", "Background")
    dlg.set_default_response("inline")
    dlg.set_close_response("cancel")

    def _resp(_dlg, response):
        if response == "inline":
            on_insert(f"![]({rel})")
        elif response == "bg":
            on_insert(f"![bg]({rel})")

    dlg.connect("response", _resp)
    dlg.present(parent)


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
