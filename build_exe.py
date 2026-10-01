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

# The release version, in ONE place. It was previously hardcoded as 1.0.0 in the
# four spots below, so the shipped 1.0.1/1.0.2/1.0.3 EXEs all still reported
# 1.0.0 in their version resource - Windows shows that in the file properties,
# so a user on 1.0.3 was told they were running 1.0.0. Bump this and the
# version resource follows.
VERSION = (1, 1, 1)
VERSION_STR = ".".join(str(n) for n in VERSION)


COMMIT = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                cwd=str(ROOT), text=True).strip()

# Version metadata. Defender's ML heuristics score an EXE partly on how much it
# looks like a legitimate shipped product, and an EXE with an empty or missing
# version resource scores badly. Real strings here are not decoration - they
# change how the binary is treated.
# PyInstaller deserializes this file by eval()-ing it in its own module
# namespace, where FixedFileInfo / StringStruct / ... are bound as bare names.
# So it must read `FixedFileInfo(...)`, NOT `ffi.FixedFileInfo(...)` - there is
# no `ffi` in that namespace and the dotted form dies with a NameError.
VERSION_FILE = ROOT / "build_version.txt"
VERSION_FILE.write_text(
    "# UTF-8\n"
    "VSVersionInfo(\n"
    "  FixedFileInfo(\n"
    f"    filevers=({VERSION[0]}, {VERSION[1]}, {VERSION[2]}, 0),\n"
    f"    prodvers=({VERSION[0]}, {VERSION[1]}, {VERSION[2]}, 0),\n"
    "    mask=0x3f,\n"
    "    flags=0x0,\n"
    "    OS=0x40004,\n"
    "    fileType=0x1,\n"
    "    subtype=0x0,\n"
    "    date=(0, 0)),\n"
    "  kids=[\n"
    "    StringFileInfo([\n"
    "      StringTable('040904B0', [\n"
    "        StringStruct('CompanyName', 'wmp-fai-server'),\n"
    "        StringStruct('FileDescription', 'WMP Find Album Information metadata server'),\n"
    f"        StringStruct('FileVersion', '{VERSION_STR}'),\n"
    "        StringStruct('InternalName', 'WMP-FAI-Server'),\n"
    "        StringStruct('LegalCopyright', 'MIT licensed'),\n"
    "        StringStruct('OriginalFilename', 'WMP-FAI-Server.exe'),\n"
    "        StringStruct('ProductName', 'WMP FAI Metadata Server'),\n"
    f"        StringStruct('ProductVersion', '{VERSION_STR}')])]),\n"
    "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
    "])\n",
    encoding="utf-8")


def main():
    if not ENTRY.exists():
        raise SystemExit(f"missing entry point: {ENTRY}")

    for stale in (ROOT / "build", DIST):
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)

    # ONE-DIRECTORY, not one-file. A one-file build unpacks itself into a fresh
    # %TEMP%\_MEIxxxxxx folder on every launch. That is one of the loudest
    # behavioural signals a Windows ML heuristic has - it is precisely what
    # droppers and crypters do, and it is why a PyInstaller onefile exe trips
    # Trojan:*!ml so reliably. Microsoft Defender flagged the 1.0.0 onefile
    # build as Trojan:Win32/Sabsik.TE.A!ml on download, which would hit every
    # single user of the release. The directory build leaves a normal exe beside
    # its dependencies, so there is nothing to unpack.
    #
    # The trade-off is that users get a folder rather than one file, so the
    # build zips it and the ZIP is what gets attached to the release.
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onedir",
        "--console",
        "--name", NAME,
        "--version-file", str(VERSION_FILE),
        # No "--": PyInstaller's parser rejects one, and the entry path is a
        # single argv element so its spaces and parentheses are already safe.
        str(ENTRY),
    ]
    print(" ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        raise SystemExit(f"PyInstaller failed with exit code {rc}")

    exe = DIST / NAME / f"{NAME}.exe"
    if not exe.exists():
        raise SystemExit(f"build reported success but {exe} is missing")
    mb = exe.stat().st_size / (1024 * 1024)
    print(f"\nbuilt {exe}  ({mb:.1f} MB exe, onedir)")
    print(f"built from commit {COMMIT}")


if __name__ == "__main__":
    main()
