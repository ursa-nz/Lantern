# Lantern

A simple GUI for authoring [Marp](https://marp.app/) slides on GNOME. Edit on the left, see the slide render on the right. Present from the same window, or in full screen. The whole deck saves as one (zip) file.

Named after the magic lantern: the 1600s slide projector that came two centuries before cinema, where a presenter would burn an oil lamp behind painted glass slides and talk, sing or play music to the projected image. Same idea, a bit *cooler* (sorry).

## Standing on shoulders

Lantern doesn't render slides, parse Markdown, draw the window, or make PDFs. Other people's software does all that. Lantern just wires it together behind a GUI.

Sincere thanks to:
- [Marp](https://marp.app/),
- [GTK 4](https://gtk.org/),
- [libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/),
- [GtkSourceView](https://gitlab.gnome.org/GNOME/gtksourceview),
- [WebKitGTK](https://webkitgtk.org/),
- [pandoc](https://pandoc.org/),
- [Node.js](https://nodejs.org/),
- [Flatpak](https://flatpak.org/),
- [IBM Plex Mono](https://www.ibm.com/plex/) (bundled under the [SIL OFL 1.1](data/fonts/IBMPlexMono/LICENSE.txt) for the editor typography), and
- the Lantern icon, adapted (with light modifications) from an illustration by [leedanii on Unsplash](https://unsplash.com/@leedanii/illustrations).

## Just want to run it

Each tagged release ships ready-to-install bundles on the [latest release](https://lab.ursa.nz/ursa.nz/Lantern/-/releases/permalink/latest). Pick whichever suits your system.

**Flatpak** (most distros). Grab `nz.ursa.Lantern.flatpak`, then:

```
flatpak remote-add --if-not-exists ursa https://software.ursa.nz/flatpak/ursa.flatpakrepo
flatpak install ursa nz.ursa.Lantern
flatpak run nz.ursa.Lantern
```

**AppImage** (any recent distro, nothing to install). Grab `Lantern-x86_64.AppImage` (or `-aarch64`):

```
chmod +x ~/Downloads/Lantern-x86_64.AppImage
~/Downloads/Lantern-x86_64.AppImage
```

It carries its own GTK, WebKit, Python, Node, marp, and pandoc, so it runs on a machine that has none of them. It's built on a Ubuntu 24.04 image, so it needs that vintage of glibc (2.39) or newer.

## Build it yourself

**Flatpak.** For reproducing what the release ships, or making your own changes. Needs `flatpak-builder`, which apt and dnf both call by that name.

```
./scripts/build-flatpak.sh
flatpak install --user build/nz.ursa.Lantern.flatpak
flatpak run nz.ursa.Lantern
```

**AppImage (self-contained).** Bundles the whole GTK/WebKit/Python/Node stack, so it runs anywhere. Build it on Debian or Ubuntu, on the oldest release you want to support. The AppImage takes its glibc floor from the build host, and the releases use Ubuntu 24.04, the first with WebKitGTK 6.0. The script also looks for `gdk-pixbuf-query-loaders` under the Debian multiarch library path, which Fedora neither names nor places the same way.

```
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
                 gir1.2-gtksource-5 gir1.2-webkit-6.0 \
                 libglib2.0-bin libgtk-3-bin libgdk-pixbuf2.0-bin \
                 librsvg2-common nodejs npm pandoc
./scripts/build-appimage.sh
./build/Lantern-x86_64.AppImage
```

**Local install (for hacking on the Python).** PDF and HTML export work. PPTX needs `pandoc` on your PATH.

On Debian or Ubuntu:

```
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
                 gir1.2-gtksource-5 gir1.2-webkit-6.0 \
                 nodejs npm pandoc flatpak xdg-utils
./scripts/install-local.sh    # ./scripts/uninstall-local.sh to remove
```

On Fedora:

```
sudo dnf install python3-gobject gtk4 libadwaita \
                 gtksourceview5 webkitgtk6.0 \
                 nodejs24 nodejs24-npm pandoc flatpak xdg-utils
./scripts/install-local.sh    # ./scripts/uninstall-local.sh to remove
```

## Using it

- New makes a `.lantern` deck. Open a `.lantern`, or open a `.md` to import it.
- Recent decks show on the welcome screen.
- Layout toggle in the header. Editor, split, or preview.
- The preview follows your cursor to the slide you are editing. Turn that off in Preferences.
- Drop an image onto the editor to add it. A dialog sets placement, size, and filters.
- Resources button opens a floating window for the deck's theme, images, and fonts.
- Pick a theme in Resources. A Marp theme, one Lantern ships (Ink or Dusk), or your own. Edit CSS opens it in your editor.
- Set a font as the body, heading, or monospace face. Lantern builds the theme and the preview updates live.
- Properties sets the deck's title and author. Preferences holds your default author name.
- Present in a window. Borderless, keeps the current size, good for Zoom.
- Present fullscreen with F5. Escape exits either present mode.
- Export to HTML, PDF, or PPTX from the burger menu.
- Ctrl+S saves the `.lantern`. Autosave keeps the preview live.

## Layout

```
src/lantern/      Python GTK app
src/lantern.in    Launcher template
data/             Desktop entry, AppStream metainfo, icon
flatpak/          flatpak-builder manifest
scripts/          install-local, build-flatpak, build-appimage
```

## Status

It all works. Create a deck, open one, or import a `.md`. Edit with a live preview that follows your cursor, and autosave keeps it current. The whole deck saves as one `.lantern` file. Drag in images and fonts with a placement dialog. Pick a theme and edit its CSS. Set fonts for the body, headings, and code. Name a deck and reopen recent ones. Present in a window or full screen, and export to HTML, PDF, and PPTX.

## Licence

Copyright © 2026 ursa.nz. Released under the GNU General Public License, version 3.0 or later. See `LICENSE` for the full text.
