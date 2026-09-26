#!/usr/bin/env python3
"""Checks the terminal interface of the installed Flatpak, on a pseudo-terminal.

    python3 flatpak/tui_smoke_test.py

Starts `flatpak run io.github.derikatwork.AudioMagic --tui`, adds a test tone
through the Add input dialog, records a few seconds, opens the help and quits.
The take must be saved and the terminal handed back. Needs a running PipeWire
and the Flatpak installed; exits non-zero if anything is wrong.
"""
import fcntl
import glob
import json
import os
import pty
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time

APP = "io.github.derikatwork.AudioMagic"
WORK = os.path.expanduser("~/audiomagic-tui-smoke")  # inside the home folder, which the sandbox can see
failures = []


def check(ok, what):
    print(("ok    " if ok else "FAIL  ") + what, flush=True)
    if not ok:
        failures.append(what)
    return ok


class Terminal:
    def __init__(self, argv, env):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
        self.proc = subprocess.Popen(argv, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        self.out = bytearray()

    def pump(self, secs):
        end = time.time() + secs
        while time.time() < end and self.proc.poll() is None:
            if select.select([self.master], [], [], 0.05)[0]:
                try:
                    self.out += os.read(self.master, 65536)
                except OSError:
                    break

    def wait_for(self, text, timeout, since=None):
        """Waits until text has been drawn (since the output position `since`, or from now)."""
        mark = len(self.out) if since is None else since
        end = time.time() + timeout
        while time.time() < end and self.proc.poll() is None:
            self.pump(0.2)
            if text in self.out[mark:]:
                return True
        return text in self.out[mark:]

    def send(self, keys, then=1.0):
        os.write(self.master, keys.encode())
        self.pump(then)


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    projects = os.path.join(WORK, "projects")
    # its own settings too: they remember the last project, which would be the other smoke test's
    config = os.path.join(WORK, "config")
    # AUDIOMAGIC_CMD runs something else instead (e.g. "python3 -m audiomagic", to test this script)
    cmd = shlex.split(os.environ.get(
        "AUDIOMAGIC_CMD", f"flatpak run --env=AUDIOMAGIC_ROOT={projects} --env=XDG_CONFIG_HOME={config} {APP}"))
    env = dict(os.environ, AUDIOMAGIC_ROOT=projects, XDG_CONFIG_HOME=config, TERM="xterm-256color")
    term = Terminal(cmd + ["--tui"], env)
    try:
        # this line appears once the engine's first state is on screen
        check(term.wait_for(b"No inputs yet", 60), "the terminal interface drew its screen")
        mark = len(term.out)
        term.send("i", 2)
        term.send("6", 1)          # the test tone tab
        term.send("\r", 3)         # add it (440 Hz)
        check(term.wait_for(b"live", 15, since=mark), "the test tone input is live")
        term.send("r", 4)          # record...
        term.send("r", 3)          # ...and stop
        mark = len(term.out)
        term.send("?", 1)
        check(term.wait_for(b"keys", 5, since=mark), "the help opens")
        term.send("\x1b", 1)
        term.send("q", 0)
        end = time.time() + 30
        while time.time() < end and term.proc.poll() is None:
            term.pump(0.5)
        check(term.proc.poll() == 0, f"it quit cleanly (exit code {term.proc.poll()})")
        check(b"\x1b[?1049l" in term.out, "the terminal was handed back")
    finally:
        if term.proc.poll() is None:
            if "AUDIOMAGIC_CMD" in os.environ:
                term.proc.send_signal(signal.SIGTERM)
            else:
                subprocess.run(["flatpak", "kill", APP])
            term.proc.wait(timeout=30)
        os.close(term.master)

    takes = []
    for pf in glob.glob(os.path.join(projects, "*", "project.json")):
        with open(pf, encoding="utf-8") as f:
            data = json.load(f)
        check([t["source"]["kind"] for t in data["tracks"]] == ["tone"], "the project has the test tone input")
        takes += data["takes"]
    check(len(takes) == 1 and takes[0]["state"] == "done" and takes[0]["duration"] > 2 * 48000,
          f"the take was saved ({[(t['state'], t['duration']) for t in takes]})")
    if failures:
        tail = term.out[-3000:].decode("utf-8", "replace")
        print(f"\n{len(failures)} check(s) failed. The end of the terminal output:\n{tail!r}")
        cache = os.environ.get("XDG_CACHE_HOME") if "AUDIOMAGIC_CMD" in os.environ else \
            os.path.expanduser(f"~/.var/app/{APP}/cache")
        try:
            with open(os.path.join(cache or os.path.expanduser("~/.cache"), "audiomagic", "tui.log")) as f:
                print("\nThe end of tui.log:\n" + f.read()[-3000:])
        except OSError as e:
            print(f"(no tui.log: {e})")
        sys.exit(1)
    print("\nThe terminal interface works inside the sandbox.")


if __name__ == "__main__":
    main()
