# -*- coding: utf-8 -*-
"""Build the distributable Windows executable.

    python build_exe.py

Produces ``dist/WMP-FAI-Server.exe``, a single self-contained file. Users need
nothing installed - no Python, no pip, no requirements.txt. See the "Running the
compiled EXE" section of README.md for what they DO need (Administrator, and
Windows Media Player itself).

Why one file and not one folder: a one-folder build leaves a ~40 MB ``_internal``
tree next to the exe that users must keep together and must not rename. The
single file is a single download and cannot be broken by moving it.

Why the console is kept: this process is a server. The startup banner
("... READY"), any port-80 bind failure and any HTTPS warning all go to the
console, and an EXE that vanishes silently on double-click is far worse than one
that shows a window. Use --noconsole only if you have another way to see errors.

``cryptography`` supplies a PyInstaller hook, so the CA/certificate generation and
the TLS listener need no extra hidden imports. There are no template or data
files to bundle either: every page is built with render_template_string, and the
only runtime-written files (certificate, key, log) go to %APPDATA%\\WMP_FAIServer.
"""
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
ENTRY = ROOT / "FAI Server.py"
NAME = "WMP-FAI-Server"
DIST = ROOT / "dist"


def main():
    if not ENTRY.exists():
        raise SystemExit(f"missing entry point: {ENTRY}")

    for stale in (ROOT / "build", DIST):
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",
        "--name", NAME,
        # No "--" separator: PyInstaller's parser does not accept one, and the
        # entry path is already a single argv element, so the spaces and the
        # parentheses in "...\New folder (4)\..." are safe.
        str(ENTRY),
    ]
    print(" ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        raise SystemExit(f"PyInstaller failed with exit code {rc}")

    exe = DIST / f"{NAME}.exe"
    if not exe.exists():
        raise SystemExit(f"build reported success but {exe} is missing")
    mb = exe.stat().st_size / (1024 * 1024)
    print(f"\nbuilt {exe}  ({mb:.1f} MB)")


if __name__ == "__main__":
    main()
