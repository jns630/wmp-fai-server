"""Build a TESTING executable for Windows 7 / 8 / 8.1.

Read this before assuming the output is a release candidate. It is not, and it
must never become one. The official 1.1.1 build in ../dist is the real release;
this exists only so old-Windows users can try the same code.

WHY A SEPARATE SCRIPT
---------------------
Python 3.9 dropped support for Windows 7 and 8.1 outright - it will not even
install, let alone run. So the official exe, built on 3.13, cannot start on those
systems at all. The last interpreter that still runs there is 3.8, which is why
this builds under it.

Keeping the two builds in separate scripts is the whole point. `build_exe.py`
deletes `dist/` and `build/` outright before it runs (it has to, to avoid a stale
analysis contaminating the next build). Pointing it at Python 3.8 and letting it
finish would therefore have wiped the official 1.1.1 artifact and left a
differently-targeted binary sitting in the release folder. This script writes to
`dist-win7/` and never touches `dist/`.

It deliberately does not call build_exe.py's main(): the only differences are
the interpreter, the output directory, the exe name and the version strings, and
those are all spelled out below where they can be seen.

REQUIREMENTS
------------
A Python 3.8 interpreter with flask, requests, urllib3, cryptography and
PyInstaller installed. Create one with:

    py -3.8 -m venv .venv38
    .venv38\\Scripts\\python -m pip install -U pip
    .venv38\\Scripts\\python -m pip install flask requests urllib3 cryptography pyinstaller chardet

`chardet` is not in the original list above, and it is there because of
something observed while building this: `requests` emits a
`RequestsDependencyWarning: Unable to find acceptable character detection
dependency (chardet or charset_normalizer)` on every single launch without it,
and on a test build that warning lands in the console of someone trying to
work out why their store is not appearing. `charset_normalizer` is the usual
answer, but its current releases have dropped Python 3.8, so on this
interpreter it installs and then fails to import. `chardet` is pure Python,
still supports 3.8, and satisfies `requests` properly.

Run it with that interpreter:

    .venv38\\Scripts\\python build_exe_win7.py

Do not run it with 3.13. It will produce a binary that looks correct and fails to
start on exactly the systems it exists to support.

VERSION STRINGS ARE DELIBERATELY NOT 1.1.1
-------------------------------------------
The file description says "Win7/8 TEST BUILD" and the product version carries a
-test suffix, so that a tester who has both builds can tell them apart from
Explorer properties alone, and so a stray copy is never mistaken for the
release. If you are reading this and about to strip those strings to make the
build "look official", that defeats the only safeguard this script has.
"""

import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
ENTRY = ROOT / "FAI Server.py"
NAME = "WMP-FAI-Server-Win7Test"
DIST = ROOT / "dist-win7"
WORK = ROOT / "build-win7"
VERSION = (1, 1, 1)
VERSION_STR = ".".join(str(n) for n in VERSION)

COMMIT = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                 cwd=str(ROOT), text=True).strip()

if sys.version_info[:2] != (3, 8):
    sys.exit(
        f"This builds for Windows 7/8, which needs Python 3.8.\n"
        f"You are running {sys.version.split()[0]} on "
        f"{sys.platform}. Use the .venv38 interpreter - see the docstring."
    )

VERSION_FILE = ROOT / "build_version_win7.txt"
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
    "        StringStruct('FileDescription', 'WMP FAI metadata server - Win7/8 TEST BUILD'),\n"
    f"        StringStruct('FileVersion', '{VERSION_STR}-test'),\n"
    f"        StringStruct('InternalName', '{NAME}'),\n"
    "        StringStruct('LegalCopyright', 'MIT licensed'),\n"
    f"        StringStruct('OriginalFilename', '{NAME}.exe'),\n"
    "        StringStruct('ProductName', 'WMP FAI Metadata Server (TEST)'),\n"
    f"        StringStruct('ProductVersion', '{VERSION_STR}-test')])]),\n"
    "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
    "])\n",
    encoding="utf-8")


def main():
    if not ENTRY.exists():
        raise SystemExit(f"missing entry point: {ENTRY}")

    # Scoped to the win7 paths only. build_exe.py's `build/` and `dist/` are left
    # alone, so the official release build survives building this.
    for stale in (WORK, DIST):
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)

    # --onedir for the same reason as the release build: a onefile exe unpacks
    # itself into a fresh %TEMP% folder on every launch, which is one of the
    # loudest behavioural signals a Windows ML heuristic has.
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onedir",
        "--console",
        "--name", NAME,
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(WORK),
        "--version-file", str(VERSION_FILE),
        # online_store.ini is DATA, not an import, so PyInstaller's dependency
        # analysis cannot see it. Same reason and same fix as build_exe.py.
        # "." is the data root, which is _internal\ in an onedir build.
        "--add-data", "%s;." % (ROOT / "online_store.ini"),
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

    # Ship the registration script beside the EXE. It is not bundled INTO the
    # binary: it has to stay an editable text file the user can read before
    # running, and it has to live next to the EXE because that is how it finds
    # it. Everything this project added to the Windows 7 workflow has to be in
    # the folder, or the tester ends up on a Windows 7 machine with a store
    # they cannot register.
    installer = ROOT / "install-online-store-win7.bat"
    if not installer.exists():
        raise SystemExit(f"missing registration script: {installer}")
    out_dir = DIST / NAME
    shutil.copy2(installer, out_dir / installer.name)

    # A run of the installer leaves a record of the hosts entries IT added, so
    # that uninstall can remove exactly those. That file is per-machine state:
    # if one is present here it describes edits made on the machine that built
    # this, and shipping it means the tester on Windows 7 uninstalls hosts
    # entries this script never added - the precise damage the record exists to
    # prevent, arriving as a "clean" build. It must never ship.
    stale_record = out_dir / "online-store-hosts-installed.txt"
    if stale_record.exists():
        stale_record.unlink()
        print(f"removed stale {stale_record.name} (per-machine installer state)")

    # The Discogs token is deliberately NOT copied into the distribution. It is
    # a credential and this artifact gets copied between machines; the build
    # says out loud that a tester has to supply their own instead.
    settings = ROOT / "local_settings.py"
    print("\nNOTE: Discogs will be OFF in this build unless the tester copies")
    print(f"      {settings.name} (containing DISCOGS_TOKEN) next to the .exe.")
    if settings.exists():
        print("      A copy exists in the source tree - copy it manually.")
    else:
        print("      No copy exists in the source tree either.")

    mb = exe.stat().st_size / (1024 * 1024)
    print(f"\nbuilt {exe}  ({mb:.1f} MB exe, onedir)")
    print(f"built from commit {COMMIT} on Python {sys.version.split()[0]}")
    print(f"shipped {installer.name} beside it")
    print("\nTEST BUILD for Windows 7/8/8.1 - not the official release.")
    print(f"The official 1.1.1 build in {ROOT / 'dist'} was not touched.")
    print("Untested on a real Windows 7 machine: see the notes below.")


if __name__ == "__main__":
    main()