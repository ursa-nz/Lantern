# Lantern

A small GUI for authoring [Marp](https://marp.app/) slides on GNOME. Edit on the left, see the slide render on the right, present from the same window. The whole deck saves as one file.

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
- [IBM Plex Mono](https://www.ibm.com/plex/) — bundled under the [SIL OFL 1.1](data/fonts/IBMPlexMono/LICENSE.txt) for the editor typography, and
- the Lantern icon, adapted (with light modifications) from an illustration by [leedanii on Unsplash](https://unsplash.com/@leedanii/illustrations).

## Just want to run it

Each tagged release ships a ready-to-install bundle. Grab `nz.ursa.Lantern.flatpak` from the [latest release](https://github.com/ursa-nz/Lantern/releases/latest), then:

```
flatpak install --user ~/Downloads/nz.ursa.Lantern.flatpak
flatpak run nz.ursa.Lantern
```

(You need `flatpak` itself installed: `sudo apt install flatpak` on Debian/Ubuntu, similar for other distros.)

## Build it yourself

**Flatpak.** For reproducing what the release ships, or making your own changes.

```
sudo apt install flatpak-builder
./scripts/build-flatpak.sh
flatpak install --user build/nz.ursa.Lantern.flatpak
flatpak run nz.ursa.Lantern
```

**Local install (for hacking on the Python).** PDF and HTML export work. PPTX needs `pandoc` on your PATH.

```
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
                 gir1.2-gtksource-5 gir1.2-webkit-6.0 \
                 nodejs npm pandoc flatpak xdg-utils
./scripts/install-local.sh    # ./scripts/uninstall-local.sh to remove
```

## Using it

- New makes a `.lantern` deck. Open a `.lantern`, or open a `.md` to import it.
- Recent decks show on the welcome screen.
- Layout toggle in the header: editor / split / preview.
- Drop an image onto the editor to add it. Lantern asks inline or background.
- Resources button: a floating window for the deck's images and fonts.
- Properties sets the deck's title and author. Preferences holds your default author name.
- Present in a window: borderless, keeps the current size (good for Zoom).
- Present fullscreen with F5. Escape exits either present mode.
- Export to HTML, PDF, or PPTX from the burger menu.
- Ctrl+S saves the `.lantern`. Autosave keeps the preview live.

## Layout

```
src/lantern/      Python GTK app
src/lantern.in    Launcher template
data/             Desktop entry, AppStream metainfo, icon
flatpak/          flatpak-builder manifest
scripts/          install-local, uninstall-local, build-flatpak
```

## Status

Open, new, edit, live preview, autosave, two present modes, export to HTML, PDF, and PPTX, `.lantern` bundles, drag in images and fonts, recent files, deck title and author. All working. Not built yet: a styles picker, PNG/JPEG export.

## License

Copyright © 2026 ursa.nz. Released under the GNU General Public License, version 3.0 or later. See `LICENSE` for the full text.
