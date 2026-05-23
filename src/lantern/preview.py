# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""Preview pane — WebKitGTK 6 view.

- Preview: wraps WebKit.WebView, exposes load_url() and a placeholder
  HTML page for the empty state.

Marp's --server mode injects its own live-reload script into the rendered
HTML, so when autosave writes a change to disk the view refreshes itself
over a websocket — no nudging from us required.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

from gi.repository import WebKit


class Preview:
    """WebKitGTK 6 wrapper.

    Marp's --server injects a live-reload script into every page it
    serves, so once we point load_uri at the file URL the view refreshes
    itself when the .md file changes — no manual reload calls needed.
    """

    def __init__(self) -> None:
        self.web_view = WebKit.WebView()
        # The embedded browser only ever loads our local marp server, so
        # clipboard JS access and DevTools would be misfeatures.
        settings = self.web_view.get_settings()
        settings.set_javascript_can_access_clipboard(False)
        settings.set_enable_developer_extras(False)
        settings.set_enable_smooth_scrolling(True)

        self.web_view.set_hexpand(True)
        self.web_view.set_vexpand(True)
        # WebView is itself a Gtk.Widget, so no scroller wrapper needed —
        # the page handles its own scrolling internally.
        self.widget = self.web_view

        self._current_url: str | None = None
        self.load_placeholder()

    def load_url(self, url: str) -> None:
        """Point the preview at `url`, replacing whatever's loaded."""
        self._current_url = url
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

    def load_placeholder(self, message: str = "") -> None:
        """Show a dim card with `message` (or a default hint when empty)."""
        msg = message or "Preview will appear once a file is open."
        # The base URI ("about:blank") only matters for resolving relative
        # URLs in the HTML, and we have none.
        html = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  html, body {{ margin: 0; height: 100%; }}
  body {{
    display: flex; align-items: center; justify-content: center;
    background: #fafafa; color: #888;
    font: 14px/1.5 system-ui, -apple-system, sans-serif;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #1e1e1e; color: #777; }}
  }}
</style></head><body><div>{msg}</div></body></html>"""
        self.web_view.load_html(html, "about:blank")
