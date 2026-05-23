# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""Adw.Application subclass and the app-wide actions / CSS.

- LanternApp: owns the application lifecycle, dispatches files passed on
  the command line via do_open, and registers the four app-level actions
  (quit, new, open, about).
- _load_css: installs an application-scoped CSS provider that sets IBM
  Plex Mono on widgets carrying the `.lantern-editor` class.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

import os
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, Gtk

from lantern import APP_ID, APP_NAME, __version__
from lantern.window import LanternWindow


class LanternApp(Adw.Application):
    def __init__(self) -> None:
        # HANDLES_OPEN tells GApplication we know what to do when the OS
        # passes us file paths (on the command line, or via the desktop
        # entry's %F).  Without it, those would be ignored.
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self._setup_actions()

    # Adw.Application's three lifecycle hooks below are called in this
    # order: do_startup once when the app boots, then either do_activate
    # (no files) or do_open (one or more files passed in).
    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self._register_fonts()
        self._load_css()

    def do_activate(self) -> None:
        # Re-focus the existing window if there is one; otherwise create.
        win = self.get_active_window() or LanternWindow(application=self)
        win.present()

    def do_open(self, files, n_files, hint) -> None:
        # One window per file passed in — mirrors typical text-editor UX
        # where opening multiple files spawns one window each.
        for f in files:
            win = LanternWindow(application=self)
            win.present()
            path = f.get_path()
            if path:
                win.open_path(path)

    # ---------- actions ----------
    def _setup_actions(self) -> None:
        # GAction is GTK's way of separating "what can the app do" from
        # "how is it triggered" — once an action is registered, menu
        # items and accelerators both invoke it by name ("app.new" etc).
        specs = (
            ("quit",  self._on_quit,  ["<primary>q"]),
            ("new",   self._on_new,   ["<primary>n"]),
            ("open",  self._on_open,  ["<primary>o"]),
            ("about", self._on_about, None),
        )
        for name, handler, accels in specs:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
            if accels:
                # <primary> is Ctrl on Linux/Windows, Cmd on macOS — Gtk
                # picks the right modifier per platform.
                self.set_accels_for_action(f"app.{name}", accels)

    def _on_quit(self, *_):
        self.quit()

    def _on_new(self, *_):
        self._foreground_window().action_new_file()

    def _on_open(self, *_):
        self._foreground_window().action_open_file()

    def _on_about(self, *_):
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name="ursa.nz",
            version=__version__,
            comments="Markdown slide authoring with live preview, powered by Marp.",
            website="https://forge.ursa.nz",
            issue_url="https://ursa.nz/contact/",
            license_type=Gtk.License.GPL_3_0,
        )
        about.present(self._foreground_window())

    def _foreground_window(self) -> "LanternWindow":
        win = self.get_active_window()
        if not win:
            win = LanternWindow(application=self)
            win.present()
        return win

    # ---------- fonts + css ----------
    def _register_fonts(self) -> None:
        """Ensure bundled fonts are visible to fontconfig.

        Flatpak: /app/share/fonts is already in fontconfig's path.
        Local dev: ~/.local/share/fonts is on the standard XDG path; we trust
        that install-local.sh ran fc-cache after copying the fonts.
        """
        return

    def _load_css(self) -> None:
        # .lantern-editor is applied to the GtkSource.View (a GtkTextView).
        # GtkTextView's text content is a child CSS node named `text`.
        css = b"""
        .lantern-editor,
        .lantern-editor text {
            font-family: "IBM Plex Mono", monospace;
            font-size: 11pt;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
