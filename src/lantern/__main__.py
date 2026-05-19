# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""Entry point for `python -m lantern`.

Constructs LanternApp and hands argv to its main loop.  Part of Lantern,
released under the GNU General Public License version 3 or later.
"""

import sys

from lantern.application import LanternApp


def main() -> int:
    # Adw.Application.run() blocks until the last window closes and
    # returns the app's exit code, which we hand back to the shell.
    return LanternApp().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
