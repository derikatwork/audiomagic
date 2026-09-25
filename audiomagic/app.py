"""Start AudioMagic: the engine, the local server, and the app window."""

import argparse
import fcntl
import logging
import os
import signal
import sys
import threading
import webbrowser

from . import __version__
from .util import log

ICON = os.path.join(os.path.dirname(__file__), "web", "img", "icon.svg")

# Set by SIGINT/SIGTERM (Ctrl+C, logging out, shutting down): quit cleanly,
# which also stops and saves a recording in progress.
_quit = threading.Event()


def _on_signal(*_):
    _quit.set()


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    path = os.path.join(base, "audiomagic")
    os.makedirs(path, exist_ok=True)
    return path


def single_instance():
    """Returns an open lock file, or None if AudioMagic is already running."""
    f = open(os.path.join(cache_dir(), "instance.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def load_gtk():
    import gi
    gi.require_version("Gtk", "3.0")
    for v in ("4.1", "4.0"):
        try:
            gi.require_version("WebKit2", v)
            break
        except ValueError:
            continue
    else:
        raise ImportError("WebKit2GTK is not installed")
    from gi.repository import GLib, Gtk, WebKit2
    return GLib, Gtk, WebKit2


def run_window(url, engine, debug=False):
    GLib, Gtk, WebKit2 = load_gtk()
    GLib.set_prgname("audiomagic")
    GLib.set_application_name("AudioMagic")
    win = Gtk.Window(title="AudioMagic")
    win.set_default_size(1320, 840)
    win.set_wmclass("audiomagic", "AudioMagic")
    try:
        win.set_icon_from_file(ICON)
    except Exception:
        pass
    view = WebKit2.WebView()
    settings = view.get_settings()
    settings.set_enable_developer_extras(debug)
    settings.set_javascript_can_access_clipboard(True)
    view.load_uri(url)
    win.add(view)

    def on_delete(*_):
        if engine.recorder is not None:
            dlg = Gtk.MessageDialog(transient_for=win, modal=True, message_type=Gtk.MessageType.QUESTION,
                                    buttons=Gtk.ButtonsType.YES_NO,
                                    text="You are still recording")
            dlg.format_secondary_text("Stop the recording, save it and quit?")
            answer = dlg.run()
            dlg.destroy()
            if answer != Gtk.ResponseType.YES:
                return True
        Gtk.main_quit()
        return False

    def on_decide_policy(view, decision, kind):
        # links to other sites open in the normal browser, not inside the app
        if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION or kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            req = decision.get_navigation_action().get_request()
            target = req.get_uri() or ""
            if not target.startswith(url.split("?")[0].rstrip("/")) and not target.startswith("about:"):
                decision.ignore()
                webbrowser.open(target)
                return True
        return False

    win.connect("delete-event", on_delete)
    view.connect("decide-policy", on_decide_policy)
    def check_quit():
        if _quit.is_set():
            Gtk.main_quit()
            return False
        return True

    GLib.timeout_add(200, check_quit)  # also gives Python's signal handlers a chance to run
    if _quit.is_set():
        return
    win.show_all()
    Gtk.main()


def show_message(title, text, error=False):
    """A small GTK dialog when there's a display; silently skipped otherwise."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        d = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.OK, text=title)
        d.format_secondary_text(text)
        d.run()
        d.destroy()
    except Exception:
        pass


def wait_for_signal():
    while not _quit.wait(0.5):
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="audiomagic", description="Simple multi-input recorder built on PipeWire.")
    ap.add_argument("--browser", action="store_true", help="open in your web browser instead of the app window")
    ap.add_argument("--no-window", action="store_true", help="just run the server and print its address")
    ap.add_argument("--port", type=int, default=0, help="local port (default: any free port)")
    ap.add_argument("--debug", action="store_true", help="verbose logging and web inspector")
    ap.add_argument("--check", action="store_true", help="check that everything needed is installed, then exit")
    ap.add_argument("--version", action="version", version=f"AudioMagic {__version__}")
    args = ap.parse_args(argv)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .checks import report, run_checks
    if args.check:
        return report()
    problems, warnings, _ = run_checks()  # also loads GStreamer before anything else does
    for w in warnings:
        log.warning("%s", w)
    if problems:
        msg = "AudioMagic can't start:\n\n" + "\n".join(problems)
        print(msg, file=sys.stderr)
        show_message("AudioMagic can't start", "\n".join(problems), error=True)
        return 1

    lock = single_instance()
    if lock is None:
        print("AudioMagic is already running.", file=sys.stderr)
        show_message("AudioMagic is already running", "Switch to its window, or close it before starting it again.")
        return 1

    from .engine import Engine
    from .server import Server

    engine = Engine()
    engine.start()
    server = Server(engine, port=args.port)
    server.start_in_thread()
    url = f"{server.url}?token={server.token}"
    log.info("AudioMagic %s running at %s", __version__, server.url)
    try:
        if args.no_window:
            print(url, flush=True)
            wait_for_signal()
        elif args.browser:
            webbrowser.open(url)
            print(f"Opened in your browser. Press Ctrl+C here to quit.\n{url}", flush=True)
            wait_for_signal()
        else:
            try:
                run_window(url, engine, args.debug)
            except (ImportError, ValueError) as e:
                log.warning("app window unavailable (%s); opening in the browser instead", e)
                webbrowser.open(url)
                print(f"Press Ctrl+C here to quit.\n{url}", flush=True)
                wait_for_signal()
    finally:
        log.info("shutting down")
        server.stop()
        engine.shutdown()
        lock.close()
    return 0
