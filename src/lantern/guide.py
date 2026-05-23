# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""In-app beginner guide, shown from the Help menu.

A scrollable dialog of short sections covering the basics. How a deck is
built, themes, images, fonts, presenting, and exporting. The text is written
for Lantern's own tools, not Marp in general.

- GuideDialog: the dialog. Sections come from _SECTIONS, each a (heading, body)
  pair rendered as a title and a wrapped paragraph. Body uses light Pango
  markup, with `<tt>` for the literal Marp syntax a reader would type.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

from gi.repository import Adw, Gtk

# Short sentences, no colons or em dashes in the prose (see the README voice).
# Colons appear only inside <tt> code, where they are the real Marp syntax.
_SECTIONS = [
    ("Slides and the deck",
     "A deck is just Markdown. A line with three dashes on its own starts a new "
     "slide. The settings block fenced by dashes at the very top is the "
     "frontmatter, and it holds things like the theme and whether slides are "
     "numbered.\n\n"
     "Your whole deck saves as one <tt>.lantern</tt> file. Slides, images, "
     "fonts, and styles travel together. Rename it to <tt>.zip</tt> to look "
     "inside."),
    ("Writing a slide",
     "Headings, lists, bold, links, and code all work as plain Markdown. Type "
     "on the left and watch the slide render on the right. The preview follows "
     "your cursor, so the slide you are editing is the one on screen."),
    ("Layouts and directives",
     "Marp reads directives from the frontmatter or from HTML comments in a "
     "slide. Add <tt>paginate: true</tt> to number your slides. Tweak one slide "
     "with a comment like <tt>&lt;!-- _class: lead --&gt;</tt>. A background "
     "image can fill the slide or take one side, which gives you a two column "
     "layout."),
    ("Themes",
     "Open Resources from the header, then pick a Base theme. You get Marp's "
     "built-ins, two themes Lantern ships called Ink and Dusk, and any theme "
     "saved in the deck. Edit CSS opens the theme in your editor. Save there "
     "and the preview catches up."),
    ("Images",
     "Drop an image onto the editor, or add it in Resources. A dialog sets how "
     "it sits. Inline drops it into the text. Background fills the slide, and a "
     "left or right split puts it on one side with your words beside it. You "
     "can also set a size and a filter like blur."),
    ("Fonts",
     "Add a font in Resources, then choose what it styles. The body, the "
     "headings, and the monospace text can each have their own font. Lantern "
     "builds the theme and the preview updates live."),
    ("Presenting",
     "Press F5 to present full screen. The window button beside it gives a "
     "borderless window that keeps its size, which suits a screen share. Arrow "
     "keys move between slides. Escape leaves either mode."),
    ("Exporting",
     "Export from the menu. PDF keeps your theme and matches the preview. "
     "PowerPoint comes out editable but plain, without the theme. HTML is a "
     "self-contained web page of the deck."),
]


class GuideDialog(Adw.Dialog):
    """Scrollable beginner guide for Lantern."""

    def __init__(self) -> None:
        super().__init__(title="Lantern Guide")
        self.set_content_width(640)
        self.set_content_height(620)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20,
                      margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        for heading, body in _SECTIONS:
            title = Gtk.Label(label=heading, xalign=0.0)
            title.add_css_class("title-4")
            box.append(title)
            para = Gtk.Label(xalign=0.0, wrap=True)
            para.set_markup(body)
            box.append(para)

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(box)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(scroller)
        self.set_child(toolbar)
