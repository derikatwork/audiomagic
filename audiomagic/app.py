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

# Set by SIGINT/SIGTERM/SIGHUP (Ctrl+C, logging out, shutting down, closing
# the terminal it was started from): quit cleanly,
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


# Inside a Flatpak the window, icon and desktop file are all named after the app ID.
APP_ID = os.environ.get("FLATPAK_ID") or "audiomagic"


def load_gtk():
    """GTK 3 with WebKit2GTK 4.1/4.0, or GTK 4 with WebKitGTK 6.0 when that's all
    there is (newer Flatpak runtimes). Returns (gtk_major, GLib, Gtk, WebKit)."""
    import gi
    repo = gi.Repository.get_default()
    webkit2 = [v for v in ("4.1", "4.0") if v in repo.enumerate_versions("WebKit2")]
    use_gtk3 = webkit2 and "3.0" in repo.enumerate_versions("Gtk") and os.environ.get("AUDIOMAGIC_GTK") != "4"
    if use_gtk3:
        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", webkit2[0])
        from gi.repository import GLib, Gtk, WebKit2
        return 3, GLib, Gtk, WebKit2
    if "6.0" not in repo.enumerate_versions("WebKit") or "4.0" not in repo.enumerate_versions("Gtk"):
        raise ImportError("WebKitGTK is not installed")
    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import GLib, Gtk, WebKit
    return 4, GLib, Gtk, WebKit


def _external_links(view, WebKit, url):
    """Links to other sites open in the normal browser, not inside the app."""
    home = url.split("?")[0].rstrip("/")

    def on_decide_policy(view, decision, kind):
        if kind in (WebKit.PolicyDecisionType.NEW_WINDOW_ACTION, WebKit.PolicyDecisionType.NAVIGATION_ACTION):
            target = decision.get_navigation_action().get_request().get_uri() or ""
            if not target.startswith(home) and not target.startswith("about:"):
                decision.ignore()
                webbrowser.open(target)
                return True
        return False

    view.connect("decide-policy", on_decide_policy)


def _make_view(WebKit, url, debug):
    view = WebKit.WebView()
    settings = view.get_settings()
    settings.set_enable_developer_extras(debug)
    settings.set_javascript_can_access_clipboard(True)
    _external_links(view, WebKit, url)
    view.load_uri(url)
    return view


def run_window(url, engine, debug=False):
    major, GLib, Gtk, WebKit = load_gtk()
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("AudioMagic")
    if major == 4:
        return _run_window_gtk4(url, engine, debug, GLib, Gtk, WebKit)
    win = Gtk.Window(title="AudioMagic")
    win.set_default_size(1320, 840)
    win.set_wmclass(APP_ID, "AudioMagic")
    try:
        win.set_icon_from_file(ICON)
    except Exception:
        pass
    win.add(_make_view(WebKit, url, debug))

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

    win.connect("delete-event", on_delete)

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


def _run_window_gtk4(url, engine, debug, GLib, Gtk, WebKit):
    Gtk.init()
    win = Gtk.Window(title="AudioMagic")
    win.set_default_size(1320, 840)
    win.set_icon_name(APP_ID)
    win.set_child(_make_view(WebKit, url, debug))
    loop = GLib.MainLoop()
    confirmed = []

    def on_close(_win):
        if engine.recorder is not None and not confirmed:
            dlg = Gtk.AlertDialog(message="You are still recording", detail="Stop the recording, save it and quit?",
                                  buttons=["Keep recording", "Stop and quit"], cancel_button=0, default_button=1)

            def answered(d, result):
                try:
                    choice = d.choose_finish(result)
                except GLib.Error:
                    return
                if choice == 1:
                    confirmed.append(True)
                    loop.quit()

            dlg.choose(win, None, answered)
            return True
        loop.quit()
        return False

    win.connect("close-request", on_close)

    def check_quit():
        if _quit.is_set():
            loop.quit()
            return False
        return True

    GLib.timeout_add(200, check_quit)
    if _quit.is_set():
        return
    win.present()
    loop.run()
    win.destroy()


def show_message(title, text, error=False):
    """A small dialog when there's a display; silently skipped otherwise."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    try:
        major, GLib, Gtk, _ = load_gtk()
        if major == 3:
            d = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO,
                                  buttons=Gtk.ButtonsType.OK, text=title)
            d.format_secondary_text(text)
            d.run()
            d.destroy()
        else:
            Gtk.init()
            loop = GLib.MainLoop()
            Gtk.AlertDialog(message=title, detail=text).choose(None, None, lambda *_: loop.quit())
            loop.run()
    except Exception:
        pass


def wait_for_signal():
    while not _quit.wait(0.5):
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(prog="audiomagic", description="Simple multi-input recorder built on PipeWire.")
    ap.add_argument("--browser", action="store_true", help="open in your web browser instead of the app window")
    ap.add_argument("--tui", action="store_true", help="run in this terminal instead of a window")
    ap.add_argument("--no-window", action="store_true", help="just run the server and print its address")
    ap.add_argument("--port", type=int, default=0, help="local port (default: any free port)")
    ap.add_argument("--debug", action="store_true", help="verbose logging and web inspector")
    ap.add_argument("--check", action="store_true", help="check that everything needed is installed, then exit")
    ap.add_argument("--version", action="version", version=f"AudioMagic {__version__}")
    args = ap.parse_args(argv)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _on_signal)

    log_path = None
    if args.tui:
        # install.sh puts Textual in its own folder, loaded only here, so the
        # window never depends on it
        vendor = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
        if os.path.isdir(vendor):
            sys.path.insert(0, vendor)
        try:
            from . import tui
        except ImportError as e:
            print(f"The terminal interface needs Textual ({e}). Run ./install.sh again, or:\n"
                  "  python3 -m pip install --require-hashes -r requirements-tui.txt", file=sys.stderr)
            return 1
        # the terminal belongs to the interface, so log to a file
        log_path = tui.log_file(cache_dir())
        tui.setup_logging(log_path, args.debug)
    else:
        logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .checks import report, run_checks
    if args.check:
        return report()
    problems, warnings, _ = run_checks()  # also loads GStreamer before anything else does
    if args.tui:
        warnings = [w for w in warnings if "WebKitGTK" not in w]  # no window needed
    for w in warnings:
        log.warning("%s", w)
    if problems:
        msg = "AudioMagic can't start:\n\n" + "\n".join(problems)
        print(msg, file=sys.stderr)
        if not args.tui:
            show_message("AudioMagic can't start", "\n".join(problems), error=True)
        return 1

    lock = single_instance()
    if lock is None:
        print("AudioMagic is already running.", file=sys.stderr)
        if not args.tui:
            show_message("AudioMagic is already running",
                         "Switch to its window, or close it before starting it again.")
        return 1

    if args.tui:
        try:
            return tui.run(_quit, warnings, log_path, args.debug)
        finally:
            lock.close()

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
