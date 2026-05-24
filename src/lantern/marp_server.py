# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 ursa.nz
"""Lifecycle for the `marp --server` subprocess that backs the preview pane.

- MarpServer: one server per editor window, scoped to the directory the
  open file lives in.  Picks a free port, waits for the socket to bind,
  exposes url_for() for the preview to load, and cleans the process up
  on stop().  Server is reused when the working directory doesn't change.

The bundled marp binary is found via a standard chain: explicit
LANTERN_MARP_BIN env var, then /app/bin/marp (flatpak), then the local
~/.local/share/lantern/node_modules/.bin/marp, then PATH.

Two marp-cli quirks the code accommodates:
- The port is set via the PORT env var; there is no --port CLI flag,
  and passing one is silently ignored.
- url_for() uses the source .md filename directly — marp v4's server
  routes by source name and renders on the fly, so no .html extension.

Part of Lantern, released under the GNU General Public License v3 or later.
"""

import os
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path


def find_marp_bin() -> str | None:
    """Locate a usable marp binary, preferring bundled over system.

    The lookup chain: an explicit LANTERN_MARP_BIN override (handy for dev),
    then the flatpak's /app/bin/marp, then a local-dev install, then whatever
    is on PATH. Shared by the preview server and the exporters.
    """
    env_bin = os.environ.get("LANTERN_MARP_BIN")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    if os.path.isfile("/app/bin/marp"):
        return "/app/bin/marp"
    # Local dev install (install-local.sh drops it here).
    local = Path.home() / ".local/share/lantern/node_modules/.bin/marp"
    if local.is_file():
        return str(local)
    # Last resort: whatever's on PATH.
    return shutil.which("marp")


class MarpServer:
    """Manages a `marp --server <directory>` subprocess.

    Marp's server mode watches the directory and pushes live-reload to
    connected clients (including our WebKitGTK preview).  We start one
    server per editor window and reuse it as long as the working directory
    doesn't change; opening a file in a new directory restarts it.
    """

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.port: int | None = None
        self.directory: str | None = None
        # marp's stderr is drained on a daemon thread (see start_for_directory);
        # the last few lines are kept here for a startup-failure diagnostic.
        self._stderr_tail: deque = deque(maxlen=10)
        self._stderr_thread: threading.Thread | None = None

    # ---------- lifecycle ----------
    def start_for_directory(self, directory) -> None:
        """Ensure a marp server is running for `directory`.

        No-op when we're already serving the same directory and the
        subprocess is still alive.  Otherwise tear down the old one
        and start fresh.
        """
        directory = str(Path(directory).expanduser().resolve())
        if self.directory == directory and self._alive():
            return
        self.stop()

        marp_bin = find_marp_bin()
        if not marp_bin:
            raise RuntimeError(
                "marp binary not found. Run scripts/install-local.sh or the "
                "flatpak build to provision dependencies."
            )

        port = self._pick_free_port()
        env = os.environ.copy()
        env["FORCE_COLOR"] = "0"
        # marp-cli reads the server port from the PORT env var; there is
        # no --port flag.  Passing one is silently ignored and marp
        # falls back to 8080, which collides if multiple windows are open.
        env["PORT"] = str(port)

        # marp logs to stderr (one "<deck> processed." line per render) and
        # nothing to stdout. Left unread, the OS pipe buffer (~64KB) fills after
        # a few thousand renders and marp blocks mid-render, wedging the live
        # preview. So pipe stderr and drain it continuously on a daemon thread,
        # keeping only the last few lines for a startup-failure diagnostic.
        self.process = subprocess.Popen(
            [marp_bin, "--server", directory],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            # Close stdin: if marp inherits an open stdin pipe it waits on it
            # for piped Markdown and never starts the server (so the preview
            # would just time out). A desktop launch hands us /dev/null, but
            # any pipe-bearing parent — a terminal launch, a wrapper — wouldn't.
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=directory,
            text=True,
        )
        self._stderr_tail = deque(maxlen=10)
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self.process,), daemon=True)
        self._stderr_thread.start()
        try:
            self._wait_for_port(port, timeout=15)
        except TimeoutError as e:
            # Stop the (possibly stuck) process, then let the drain thread flush
            # the final stderr lines on the EOF that follows and read its tail.
            self.stop()
            self._stderr_thread.join(timeout=0.5)
            tail = " | ".join(list(self._stderr_tail)[-3:])
            if tail:
                raise TimeoutError(f"{e}. marp said {tail}") from None
            raise

        self.port = port
        self.directory = directory

    def stop(self) -> None:
        """Terminate the marp process (no-op if not running)."""
        if self.process and self.process.poll() is None:
            # SIGTERM first; give the process a few seconds to clean up.
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # Didn't exit politely — escalate to SIGKILL.
                self.process.kill()
                self.process.wait()
        self.process = None
        self.port = None
        self.directory = None

    def url_for(self, file_path) -> str:
        """Browser URL for the slide deck at `file_path`."""
        if self.port is None:
            raise RuntimeError("Server not started")
        # marp-cli v4's server routes by the source filename: GET
        # /deck.md returns rendered HTML on the fly.  URL-quote the name
        # so paths with spaces still resolve.
        from urllib.parse import quote
        name = quote(Path(file_path).name)
        return f"http://localhost:{self.port}/{name}"

    # ---------- internals ----------
    def _drain_stderr(self, proc) -> None:
        # Consume marp's stderr line by line until the process exits, so its
        # pipe never fills (see start_for_directory). Keep only the tail, in a
        # bounded deque, for the startup diagnostic.
        if not proc.stderr:
            return
        try:
            for line in proc.stderr:
                self._stderr_tail.append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass

    def _alive(self) -> bool:
        # Popen.poll() returns None while the process is still running,
        # and the exit code once it's terminated.
        return self.process is not None and self.process.poll() is None

    @staticmethod
    def _pick_free_port() -> int:
        # Bind to port 0 and the OS hands us a free ephemeral port,
        # which we close immediately and pass to marp.  There's a tiny
        # TOCTOU window where another process could grab the port
        # between our close and marp's bind, but it's vanishingly rare.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @staticmethod
    def _wait_for_port(port: int, timeout: float) -> None:
        # marp's HTTP server takes a beat to come up after launch.
        # Poll-connect every 100ms until it answers or we time out.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.1)
        raise TimeoutError(f"marp server did not bind to port {port} within {timeout}s")
