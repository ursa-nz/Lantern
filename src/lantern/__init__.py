# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""Lantern — a small GUI for authoring Marp slides on GNOME.

Top-level package: pins the gi typelib versions everything else depends on,
and exposes the constants other modules import (APP_ID, APP_NAME, version).

Lantern is free software: GNU General Public License version 3 or later.
"""

import gi

# Pin every GObject-introspection namespace we use to a known major
# version.  Doing it once here means submodules can `from gi.repository
# import X` without each one repeating require_version().  Forgetting a
# pin makes the import silently pick whatever version is installed,
# which tends to bite later with mysterious attribute errors.
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
gi.require_version("WebKit", "6.0")

__version__ = "0.9.0"
APP_ID = "nz.ursa.Lantern"
APP_NAME = "Lantern"
