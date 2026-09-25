"""Checks that everything AudioMagic needs is installed (``audiomagic --check``)."""

import importlib
import shutil
import subprocess

APT_HINT = "sudo apt install {}"

MODULES = [
    ("numpy", "python3-numpy"),
    ("scipy", "python3-scipy"),
    ("aiohttp", "python3-aiohttp"),
    ("gi", "python3-gi"),
]

ELEMENTS = [
    ("pipewiresrc", "gstreamer1.0-pipewire", True),
    ("pipewiresink", "gstreamer1.0-pipewire", True),
    ("appsink", "gstreamer1.0-plugins-base", True),
    ("audioconvert", "gstreamer1.0-plugins-base", True),
    ("uridecodebin", "gstreamer1.0-plugins-base", False),
    ("souphttpsrc", "gstreamer1.0-plugins-good", False),
    ("srtsrc", "gstreamer1.0-plugins-bad", False),
    ("avdec_aac", "gstreamer1.0-libav", False),
]


def run_checks():
    """Returns (problems, warnings, info) as lists of strings."""
    problems, warnings, info = [], [], []
    for mod, pkg in MODULES:
        try:
            importlib.import_module(mod)
        except ImportError:
            problems.append(f"Python module '{mod}' is missing: {APT_HINT.format(pkg)}")
    try:
        from .gst import Gst, has_element
        info.append(f"GStreamer {Gst.version_string().split()[-1]}")
        for el, pkg, required in ELEMENTS:
            if not has_element(el):
                msg = f"GStreamer element '{el}' is missing: {APT_HINT.format(pkg)}"
                (problems if required else warnings).append(msg)
    except (ImportError, ValueError) as e:
        problems.append(f"GStreamer Python bindings are missing ({e}): "
                        f"{APT_HINT.format('python3-gst-1.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0')}")
    if shutil.which("pw-dump") is None:
        problems.append(f"pw-dump is missing: {APT_HINT.format('pipewire-bin')}")
    else:
        r = subprocess.run(["pw-cli", "info", "0"], capture_output=True, text=True) if shutil.which("pw-cli") else None
        if r is not None and r.returncode != 0:
            problems.append("PipeWire is not running for this user (check: systemctl --user status pipewire)")
        elif r is not None:
            for line in r.stdout.splitlines():
                if "version" in line and '"' in line:
                    version = line.split('"')[1]
                    info.append(f"PipeWire {version}")
                    break
    if shutil.which("ffmpeg") is None:
        warnings.append(f"ffmpeg is missing, so exporting won't work: {APT_HINT.format('ffmpeg')}")
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        try:
            gi.require_version("WebKit2", "4.1")
        except ValueError:
            gi.require_version("WebKit2", "4.0")
        from gi.repository import WebKit2  # noqa: F401
    except (ImportError, ValueError):
        warnings.append("WebKitGTK is missing, so AudioMagic will open in your web browser instead: "
                        + APT_HINT.format("gir1.2-webkit2-4.1"))
    return problems, warnings, info


def report():
    problems, warnings, info = run_checks()
    for line in info:
        print(f"  ok    {line}")
    for line in warnings:
        print(f"  warn  {line}")
    for line in problems:
        print(f"  FAIL  {line}")
    if not problems:
        print("AudioMagic has everything it needs." if not warnings else "AudioMagic will run (see warnings).")
    return 1 if problems else 0
