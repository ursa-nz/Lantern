# Lantern rework: multi-arch + portable decks

Handoff/plan doc. Written so a fresh session can pick this up without prior
context. **High-level by design** — exact APIs and CSS will shift as we
experiment our way through the WebKit PDF path (see "Live experiment"). Treat
the locked decisions as fixed and the experiment section as provisional.

## What Lantern is

A GNOME GUI for authoring [Marp](https://marp.app/) Markdown slide decks.
Repo `ursa-nz/Lantern` (forge.ursa.nz), local checkout `~/MarpLinux`. Today:

- **Preview** = a `WebKit.WebView` (`preview.py`) loading a `marp --server`
  subprocess (`marp_server.py`) that re-renders on file change.
- **Export** (`window.py`, `_run_export_worker`) shells out to marp-cli:
  `--html`, `--pdf`, `--pptx`. PDF/PPTX go through a bundled, **x86_64-only**
  `chrome-headless-shell` (set via `CHROME_PATH` in `lantern.in`).
- Ships as a single-file flatpak bundle built by `scripts/build-flatpak.sh`
  from `flatpak/nz.ursa.Lantern.yaml`. CI: `.forgejo/workflows/build-flatpak.yml`.

## Goal

1. The **app** runs on x86_64 **and** aarch64.
2. **Decks are portable** — self-contained, move as one file.

The blocker for (1): Google ships **no Linux-ARM** `chrome-headless-shell`
(confirmed — Chrome-for-Testing has only `linux64`). So Chromium must go.

## Locked decisions (do not relitigate)

- **Build for general users**, not the maintainer's machine. kDrive/sync/
  filesystem quirks are irrelevant to product architecture. Don't cosplay a
  macOS folder-bundle on Linux either.
- **Export engines:** PDF (and PNG if added) via **WebKitGTK** from marp's
  themed HTML; **PPTX via pandoc** (multi-arch, ~35 MB) — intentionally
  editable-but-unthemed ("no great loss"; marp's PPTX was just slide images
  anyway); HTML stays marp-cli. Pandoc theming, if any, is just a curated
  `--reference-doc` — it can't read marp CSS.
- **Save format = `.lantern`** — a zip with a single `.lantern` extension
  (we tell users it's just a zip; rename to `.zip` to peek inside). Started as
  `.lantern.zip` but that lost MIME detection: the `.zip` suffix pulls in the
  uncapped `application/zip` glob, which a flatpak app's glob can't outrank.
  `.lantern` sidesteps that entirely. Contents are a vanilla Marp project so
  `unzip` → any Marp tool renders it identically (interop = unzip-first):
  - `deck.md` (fixed entry name)
  - `.marprc.yml` with `themeSet: ['styles']` — **interop linchpin**: marp
    auto-loads config from cwd, so bare marp-cli registers the custom themes
    too. (Do NOT register themes via a CLI flag — see marp gotcha below.)
  - `images/`, `styles/` (custom theme CSS w/ `/* @theme name */` headers +
    bundled fonts).
  - Lifecycle: unpack to an app temp dir on open (live edits there = instant
    preview + crash recovery), re-zip atomically on explicit Save.
- **New decks** get an explicit `theme: default` frontmatter directive
  (done — `document.py:default_template`).
- **No Claude co-author trailer** on commits. Keep one shared build path
  (`build-flatpak.sh`) for local + CI.

## The three phases (one commit each)

### Phase 1 — theme directive + drop Chromium (makes the app multi-arch-capable)
- [x] `theme: default` in `default_template`.
- [x] Export reworked into a new `lantern/export.py`: PDF → WebKit print
  (`_PdfPrinter`), PPTX → pandoc subprocess, HTML → marp-cli. `window.py` now
  just hands off to `export.run(...)`; `EXPORT_FORMATS` is a 3-tuple and the
  old chromium worker is gone. **PDF path verified in-app** (themed, paginated,
  full-page, backgrounds correct).
- [x] `flatpak/nz.ursa.Lantern.yaml`: removed the `chrome-headless-shell`
  install + wrapper; added a `pandoc` module (3.9.0.2, per-arch tarballs via
  `only-arches`). Modules now: marp-cli, pandoc, lantern.
- [x] `lantern.in`: dropped the `CHROME_PATH` block.
- [x] PPTX→pandoc verified on a real install (basic deck).
- [x] CI: aarch64 builds natively on GitHub's `ubuntu-24.04-arm` runner (free
  for public repos — no QEMU); a matrix ships one bundle per arch and a
  tags-only release job attaches both. (Forgejo can't cross-build: atutahi is
  x86_64 with no aarch64 binfmt.)

### Phase 2 — `.lantern` container format (DONE)
- [x] `bundle.py`: pack/unpack/scaffold a working dir <-> .lantern
  (deck.md + `.marprc.yml` `themeSet: styles` + images/ + styles/ + an
  EPUB-style uncompressed `mimetype` marker as the first entry), atomic save,
  zip-slip guard.
- [x] `Document` is working-dir-backed: New/Open(unpack)/Import(.md)/
  write_working(autosave)/save(re-zip), two-snapshot dirty model (working
  file vs last-zipped).
- [x] `window.py`: New makes a .lantern; Open unpacks bundles or imports a
  loose .md; Save (Ctrl+S) re-zips / Save As; marp --server now watches the
  small working dir, which kills the huge-parent-dir preview lag.
- [x] File type: `application/vnd.lantern+zip`, glob `*.lantern` (no `.zip`
  suffix so it doesn't fight `application/zip`; resolves even under flatpak's
  app-glob weight cap) + content magic on the `mimetype` marker (priority 60),
  sub-class-of zip; desktop `MimeType` + mime XML + manifest install. Verified
  the glob resolves under a simulated weight cap, with and without the marker.
- Deferred: an optional `lantern.json` manifest (format version) — not needed
  yet.

### Phase 3 — resources / typography manager (DONE)
- [x] `bundle.py` assets: add_image/add_font (copy into images/ or
  styles/fonts/ with name de-dup), list_images/list_fonts, delete_asset,
  count_references (usage scan).
- [x] `resources.py`: a floating, non-modal `ResourcesWindow` (header toggle)
  with Images + Fonts lists — add via + or drop onto the window; image rows
  insert (inline vs background prompt) or delete (warns when still referenced).
- [x] Editor drop-target: drop an image on the editor → bundle it → inline/bg
  prompt → insert at cursor (`editor.insert_at_cursor`).
- Deferred to **Phase 4** (post-0.9.0): a styles editor/selector — pick from
  Marp builtins + curated styles we ship + the bundle's themes (sets the
  `theme:` directive), and "edit CSS" opens the theme in the user's editor.
  (The `@font-face`/`@import`-rewrite-on-import idea also lands with that.)

## Live experiment (PROVISIONAL — ground is shifting here)

We are choosing the **PDF engine**. Status:

- ✅ WebKit renders marp with full fidelity incl. **backgrounds**
  (`print-color-adjust: exact` works).
- ❌ WebKit does **not paginate marp's default output**: marp wraps each slide
  in `<svg><foreignObject><section>` (Chromium-tuned), and CSS
  `page-break`/`@page size` don't apply inside SVG → all slides flow onto one
  A4 page.
- **Option A — PROVEN.** Faithful, vector, themed, paginated, full-page PDF
  from marp's HTML via WebKit. No Chromium. The recipe (verified on a 2-slide
  deck incl. a full-bleed background slide):
  1. Render `marp --template bare --html --allow-local-files deck.md -o x.html`
     with `stdin=subprocess.DEVNULL`. (`bare` = static slides; marp-cli/
     Marpit v3 always inline-wraps each slide in `<svg data-marpit-svg
     viewBox="0 0 1280 720">` and `inlineSVG:false` no longer changes that.)
  2. Load `x.html` in an **offscreen WebView whose window is 1280×720** —
     WebKit lays out/prints at the *window* width, so it must equal the slide
     width or the slide prints scaled-down in the corner.
  3. Inject a print user-stylesheet forcing the **outer** svg to paginate:
     `@media print { svg[data-marpit-svg]{ display:block; break-after:page; }
     svg[data-marpit-svg]:last-of-type{ break-after:auto } }`. (Page-break on
     the inner `<section>` — what marp emits — does nothing; the outer `<svg>`
     is a block box where it works.)
  4. `WebKit.PrintOperation` → printer `"Print to File"`, `output-uri` +
     `output-file-format=pdf`; page size via `Gtk.PageSetup` custom paper
     960×540 pt (=1280×720 px). **Never** `Gtk.PrintSettings.set_paper_size()`
     — it hangs `print()`.
  - Gotchas: marp "hangs" = it waits on an inherited stdin pipe → `DEVNULL`.
    The throwaway spike (run via `--command=python3`) needs `GTK_A11Y=none` +
    `WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1` for its WebKit web process;
    **the real app won't** (it runs as `nz.ursa.Lantern` with a matching
    sandbox id — its preview WebView already works).
  - Next: wire this into `window.py` as the PDF export (offscreen WebView +
    PrintOperation, async on the main thread), and re-verify inside the real
    app (no env workarounds). Then PPTX→pandoc + drop the Chromium module.
- **Fallbacks:** C = bundle a multi-arch Chromium (~150 MB; distro/Flathub
  build may not be lib-compatible with the GNOME runtime — integration risk).
  B = WebKit snapshot per slide → assemble a Cairo PDF (reliable pagination,
  multi-arch, but **raster** — conflicts with the "typography matters"
  priority). Decision rule: invest a timeboxed effort in A; if marp can't emit
  paginatable HTML or WebKit won't size pages, fall back to C.

### Gotchas learned
- **marp config:** don't pass config via a flag (`-c`/`--config-file` get
  mis-parsed → marp waits on stdin and "hangs"). Auto-load `.marprc.yml` from
  cwd instead (also the Phase-2 mechanism).
- **WebKit print-to-file:** set the printer to `"Print to File"`, plus
  `output-uri` + `output-file-format=pdf` in `Gtk.PrintSettings`. Page size
  must be forced or it defaults to A4 (still unsolved for the SVG case).
- **Spike harness:** `~/MarpLinux/_spike_webkit_pdf.py`, run inside the flatpak
  (`flatpak run --command=python3 nz.ursa.Lantern ~/MarpLinux/_spike_webkit_pdf.py`),
  writes `~/lantern-spike.pdf`. No `Gtk.Application` (the flatpak D-Bus proxy
  only lets us own the app's own bus name). Throwaway.

## Guardrails
- Don't drift from the 3-phase sequencing.
- Keep the PDF-engine question isolated from the rest of Phase 1.
- Verify PDF fidelity by eye (render + read the PDF) — a green build ≠ correct
  output.
