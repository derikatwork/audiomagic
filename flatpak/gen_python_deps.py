#!/usr/bin/env python3
"""Write python-deps.json: the Python packages for the Flatpak, as pinned wheels.

Wheels are listed for several Python versions so the manifest keeps working
when the GNOME runtime moves to a newer Python; pip picks the matching ones.

    python3 flatpak/gen_python_deps.py    (needs pip and internet access)
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

PACKAGES = ["numpy==2.5.3", "scipy==1.18.1", "aiohttp==3.14.3", "textual==8.2.8"]
PYTHONS = ["3.12", "3.13", "3.14"]
PLATFORMS = ["manylinux_2_28_x86_64", "manylinux_2_27_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64"]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "python-deps.json")


def wheels_for(python):
    with tempfile.TemporaryDirectory() as d:
        cmd = [sys.executable, "-m", "pip", "download", "--quiet", "--only-binary=:all:",
               "--python-version", python, "--implementation", "cp", "-d", d, *PACKAGES]
        for p in PLATFORMS:
            cmd += ["--platform", p]
        subprocess.run(cmd, check=True)
        return sorted(os.listdir(d))


def pypi_file(filename):
    name, version = filename.split("-")[:2]
    meta = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json"))
    for f in meta["urls"]:
        if f["filename"] == filename:
            return {"type": "file", "url": f["url"], "sha256": f["digests"]["sha256"]}
    raise SystemExit(f"{filename} not found on PyPI")


def main():
    files = sorted({f for py in PYTHONS for f in wheels_for(py)})
    names = sorted({p.split("==")[0] for p in PACKAGES})
    module = {
        "name": "python-deps",
        "buildsystem": "simple",
        "build-commands": [
            "pip3 install --no-index --find-links=\"file://${PWD}\" --prefix=${FLATPAK_DEST} --no-build-isolation "
            + " ".join(names)
        ],
        "cleanup": ["/bin"],  # the packages' command-line tools (f2py, pygmentize, …) aren't needed
        "sources": [pypi_file(f) for f in files],
    }
    with open(OUT, "w") as f:
        json.dump(module, f, indent=2)
        f.write("\n")
    print(f"wrote {OUT}: {len(files)} wheels")


if __name__ == "__main__":
    main()
