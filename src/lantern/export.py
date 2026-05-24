# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""Export the open deck to HTML, PDF, or PPTX.

- run(fmt, md_path, out_path, on_done): dispatch an export. on_done(ok, msg)
  is always invoked back on the GTK main loop when the export settles.
- HTML and PPTX are plain subprocess calls (marp, pandoc) on a worker thread.
- PDF deliberately avoids a bundled Chromium — Google ships no Linux-ARM
  chrome-headless-shell, which would lock the app to x86_64 — and instead
  prints marp's slides through WebKitGTK, the same engine that drives the
  preview. _PdfPrinter holds the WebKit-specific dance.

Two non-obvious tactics the PDF path relies on, learned the hard way:
- marp's `--template bare` (static slides). The default bespoke template
  scales the current slide to the *window* with JavaScript, which then prints
  at that scaled-down size; bare leaves the slides at full size.
- WebKit lays out (and prints) at the view's allocated width, so the offscreen
  window is sized to the slide.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from gi.repository import GLib, Gtk, WebKit

from lantern.marp_server import find_marp_bin

# marp puts its print page-break rules on the inner <section>, which lives
# inside an <svg><foreignObject> and so never actually breaks. Force the break
# on the OUTER <svg> — a block box, where break-after is honoured.
_PRINT_CSS = """
@media print {
  html, body { margin: 0 !important; padding: 0 !important; }
  svg[data-marpit-svg] {
    display: block !important;
    break-after: page !important;
    break-inside: avoid !important;
  }
  svg[data-marpit-svg]:last-of-type { break-after: auto !important; }
}
"""

# marp's default slide is 1280x720 px (16:9); the `size:` directive can change
# it, so the real dimensions are read from the rendered HTML's @page rule.
_DEFAULT_SIZE = (1280, 720)
_PX_TO_PT = 0.75  # 96dpi CSS pixel -> 72dpi print point

_EXPORT_TIMEOUT = 180  # seconds; marp/pandoc occasionally chew on big decks


def run(fmt: str, md_path: str, out_path: str, on_done) -> None:
    """Export `md_path` to `out_path` as 'html', 'pdf', or 'pptx'.

    on_done(ok: bool, message: str) is called on the GTK main loop once the
    export finishes, succeeds, or fails — safe to touch widgets from there.
    """
    if fmt == "pdf":
        # PDF runs on the main loop (WebKit isn't thread-safe); the marp
        # pre-render inside it hops to a worker and back.
        _export_pdf(md_path, out_path, on_done)
    elif fmt == "pptx":
        _in_thread(_export_pptx, md_path, out_path, on_done)
    else:
        _in_thread(_export_html, md_path, out_path, on_done)


# ---------------------------------------------------------------------------
# Subprocess formats (HTML, PPTX)
# ---------------------------------------------------------------------------
def _in_thread(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


def _report(on_done, ok: bool, message: str) -> None:
    """Bridge a worker-thread result back to the main loop."""
    GLib.idle_add(on_done, ok, message)


def _export_html(md_path, out_path, on_done) -> None:
    marp = find_marp_bin()
    if not marp:
        _report(on_done, False, "marp binary not found")
        return
    ok, err = _subprocess(
        [marp, "--html", "--allow-local-files", md_path, "-o", out_path],
        cwd=str(Path(md_path).parent),
    )
    _done_if_written(on_done, ok, out_path, err)


def _export_pptx(md_path, out_path, on_done) -> None:
    pandoc = _find_pandoc()
    if not pandoc:
        _report(on_done, False, "pandoc not found. PPTX export needs pandoc")
        return
    # pandoc produces a native, editable deck (text boxes, not slide images).
    # It ignores the marp theme by design; that's the accepted trade.
    ok, err = _subprocess([pandoc, md_path, "-o", out_path], cwd=str(Path(md_path).parent))
    _done_if_written(on_done, ok, out_path, err)


def _done_if_written(on_done, ok: bool, out_path: str, err: str) -> None:
    if ok and Path(out_path).exists():
        _report(on_done, True, f"Exported {Path(out_path).name}")
    else:
        _report(on_done, False, f"Export failed. {err}")


def _subprocess(args, cwd) -> tuple[bool, str]:
    """Run `args`, returning (ok, last-stderr-line). stdin is closed because
    marp otherwise blocks waiting on an inherited pipe."""
    try:
        p = subprocess.run(
            args, capture_output=True, text=True, cwd=cwd,
            timeout=_EXPORT_TIMEOUT, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as e:
        return False, str(e)
    if p.returncode == 0:
        return True, ""
    tail = (p.stderr or "").strip().splitlines()
    return False, tail[-1] if tail else f"exit {p.returncode}"


def _find_pandoc() -> str | None:
    """Locate pandoc, preferring an explicit override then the flatpak bundle."""
    env_bin = os.environ.get("LANTERN_PANDOC_BIN")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    if os.path.isfile("/app/bin/pandoc"):
        return "/app/bin/pandoc"
    return shutil.which("pandoc")


# ---------------------------------------------------------------------------
# PDF via WebKit
# ---------------------------------------------------------------------------
def _export_pdf(md_path, out_path, on_done) -> None:
    def worker():
        marp = find_marp_bin()
        if not marp:
            _report(on_done, False, "marp binary not found")
            return
        tmp = Path(tempfile.mkdtemp(prefix="lantern-pdf-"))
        html = tmp / "deck.html"
        ok, err = _subprocess(
            [marp, "--template", "bare", "--html", "--allow-local-files", md_path, "-o", str(html)],
            cwd=str(Path(md_path).parent),
        )
        if not ok or not html.exists():
            shutil.rmtree(tmp, ignore_errors=True)
            _report(on_done, False, f"Export failed. {err}")
            return
        # WebKit must run on the main loop.
        GLib.idle_add(_start_pdf_print, html, out_path, on_done)

    _in_thread(worker)


def _start_pdf_print(html_path, out_path, on_done) -> bool:
    _PdfPrinter(html_path, out_path, on_done).start()
    return False  # one-shot idle source


def _read_page_size(html_path) -> tuple[int, int]:
    """Pull the slide dimensions from the rendered HTML's @page rule."""
    try:
        text = Path(html_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return _DEFAULT_SIZE
    m = re.search(r"@page\s*\{[^}]*size:\s*(\d+)px\s+(\d+)px", text)
    return (int(m.group(1)), int(m.group(2))) if m else _DEFAULT_SIZE


class _PdfPrinter:
    """Prints one marp HTML file to PDF through an offscreen WebKit view.

    GTK4 has no true offscreen window, so the view lives in a real window
    sized to the slide (WebKit prints at the view's width) but kept invisible
    via opacity 0. The window, view, and temp HTML are torn down once the
    print operation finishes or fails.
    """

    def __init__(self, html_path, out_path, on_done) -> None:
        self._html = Path(html_path)
        self._out = out_path
        self._on_done = on_done
        self._w, self._h = _read_page_size(self._html)

        ucm = WebKit.UserContentManager()
        ucm.add_style_sheet(WebKit.UserStyleSheet(
            _PRINT_CSS, WebKit.UserContentInjectedFrames.ALL_FRAMES,
            WebKit.UserStyleLevel.USER, None, None,
        ))
        self._view = WebKit.WebView(user_content_manager=ucm)
        self._win = Gtk.Window()
        self._win.set_default_size(self._w, self._h)
        self._win.set_opacity(0.0)
        self._win.set_child(self._view)

    def start(self) -> None:
        self._view.connect("load-changed", self._on_load)
        self._win.present()
        self._view.load_uri(GLib.filename_to_uri(str(self._html), None))

    def _on_load(self, _view, event) -> None:
        if event == WebKit.LoadEvent.FINISHED:
            # Let layout settle one tick before printing.
            GLib.timeout_add(150, self._do_print)

    def _do_print(self) -> bool:
        op = WebKit.PrintOperation.new(self._view)
        op.set_print_settings(self._settings())
        op.set_page_setup(self._page_setup())
        op.connect("finished", lambda *_: self._finish(True, f"Exported {Path(self._out).name}"))
        op.connect("failed", lambda _o, e: self._finish(False, f"PDF export failed. {e.message}"))
        # `print` is a builtin; gi exposes the method as print() or print_().
        (getattr(op, "print", None) or getattr(op, "print_"))()
        return False

    def _settings(self) -> Gtk.PrintSettings:
        s = Gtk.PrintSettings()
        # WebKit resolves a printer by name; "Print to File" is GTK's virtual
        # file backend. (Setting the paper size on the *settings* hangs print()
        # — it goes on the page setup below instead.)
        s.set("printer", "Print to File")
        s.set("output-uri", GLib.filename_to_uri(self._out, None))
        s.set("output-file-format", "pdf")
        return s

    def _page_setup(self) -> Gtk.PageSetup:
        ps = Gtk.PageSetup()
        ps.set_paper_size(Gtk.PaperSize.new_custom(
            "marp-slide", "Marp slide",
            self._w * _PX_TO_PT, self._h * _PX_TO_PT, Gtk.Unit.POINTS,
        ))
        for setter in ("set_top_margin", "set_bottom_margin", "set_left_margin", "set_right_margin"):
            getattr(ps, setter)(0, Gtk.Unit.POINTS)
        return ps

    def _finish(self, ok: bool, message: str) -> None:
        self._win.destroy()
        shutil.rmtree(self._html.parent, ignore_errors=True)
        self._on_done(ok, message)
