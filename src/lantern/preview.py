# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""Preview pane — WebKitGTK 6 view with a fallback status page.

- Preview: a Gtk.Stack holding the WebKit.WebView and a status page. The web
  view shows the rendered deck; the status page shows a short message when
  there is nothing to preview or the preview could not load. A Reload button
  appears on the status page only for a failure, and calls back into the
  window to rebuild the preview.

Marp's --server mode injects its own live-reload script into the rendered HTML,
so when autosave writes a change to disk the view refreshes itself over a
websocket. We only reload by hand after a theme or CSS change, or to recover.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

from gi.repository import Adw, GLib, Gtk, WebKit


class Preview:
    """WebKitGTK 6 wrapper with an error fallback.

    on_reload, if given, is called when the user clicks Reload on the status
    page. The window wires it to rebuilding the marp server and reloading.
    """

    def __init__(self, on_reload=None) -> None:
        self._on_reload = on_reload

        self.web_view = WebKit.WebView()
        # The embedded browser only ever loads our local marp server, so
        # clipboard JS access and DevTools would be misfeatures.
        settings = self.web_view.get_settings()
        settings.set_javascript_can_access_clipboard(False)
        settings.set_enable_developer_extras(False)
        settings.set_enable_smooth_scrolling(True)
        self.web_view.set_hexpand(True)
        self.web_view.set_vexpand(True)
        # A load failure (marp not serving, or the server went away) means the
        # rendered page never arrives, so fall back to the status page. A load
        # that never reports finished (a dropped or wedged load, which can
        # leave the pane blank) is caught by the watchdog armed in load_url.
        self.web_view.connect("load-failed", self._on_load_failed)
        self.web_view.connect("load-changed", self._on_load_changed)

        # Shown instead of the web view when idle or after a failure. The
        # Reload button is only revealed for a failure, so the idle state stays
        # quiet.
        self._reload_btn = Gtk.Button(label="Reload preview", halign=Gtk.Align.CENTER)
        self._reload_btn.add_css_class("pill")
        self._reload_btn.connect("clicked", lambda *_: self._reload_clicked())
        self._status = Adw.StatusPage(icon_name="x-office-presentation-symbolic")
        self._status.set_child(self._reload_btn)

        self._stack = Gtk.Stack()
        self._stack.add_named(self.web_view, "view")
        self._stack.add_named(self._status, "status")
        self.widget = self._stack

        self._current_url: str | None = None
        self._load_watchdog = 0
        self.show_idle()

    def load_url(self, url: str) -> None:
        """Point the preview at `url` and show the web view."""
        self._current_url = url
        self._stack.set_visible_child_name("view")
        self._arm_watchdog()
        self.web_view.load_uri(url)

    def reload(self) -> None:
        """Reload the current URL (no-op if nothing has been loaded yet)."""
        if self._current_url:
            self.web_view.reload()

    def goto_slide(self, index: int) -> None:
        """Show the slide at 0-based `index` in the live preview.

        marp's bespoke deck numbers slides from 1 in the URL fragment and
        navigates on hashchange, so setting the hash jumps to the slide
        without a reload. No-op until a deck has loaded.
        """
        if not self._current_url or index < 0:
            return
        self.web_view.evaluate_javascript(
            f"location.hash = '{index + 1}';", -1, None, None, None, None)

    def show_idle(self, message: str = "Preview will appear once a file is open.") -> None:
        """Show the quiet idle state, with no Reload button."""
        self._status.set_title("Preview")
        self._status.set_description(message)
        self._reload_btn.set_visible(False)
        self._stack.set_visible_child_name("status")

    def show_error(self, message: str) -> None:
        """Show the failure state with `message` and a Reload button."""
        self._status.set_title("Preview didn't load")
        self._status.set_description(message)
        self._reload_btn.set_visible(True)
        self._stack.set_visible_child_name("status")

    def _reload_clicked(self) -> None:
        if self._on_reload:
            self._on_reload()

    # ---- load watchdog: catch a load that never finishes, or one that fails -
    def _arm_watchdog(self) -> None:
        self._cancel_watchdog()
        self._load_watchdog = GLib.timeout_add_seconds(8, self._watchdog_fire)

    def _cancel_watchdog(self) -> None:
        if self._load_watchdog:
            GLib.source_remove(self._load_watchdog)
            self._load_watchdog = 0

    def _watchdog_fire(self) -> bool:
        self._load_watchdog = 0
        self.show_error("The preview is taking too long. Try reloading.")
        return GLib.SOURCE_REMOVE

    def _on_load_changed(self, _view, event) -> None:
        if event == WebKit.LoadEvent.FINISHED:
            self._cancel_watchdog()
            # A late finish, after we already showed the error, still recovers.
            self._stack.set_visible_child_name("view")

    def _on_load_failed(self, _view, _event, _uri, _error) -> bool:
        self._cancel_watchdog()
        # Returning True suppresses WebKit's own error page so ours shows.
        self.show_error("The preview server didn't respond. Try reloading.")
        return True
