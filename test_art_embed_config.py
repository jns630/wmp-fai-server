"""Which online_store.ini does a FROZEN build read?

The bug this guards: in a frozen build __file__ is inside _internal\, so a
loader that checks its own directory first always finds the BUNDLED copy and
ignores the one the user edits beside the EXE. Setting
embed_art_in_library = true beside the EXE then did nothing at all.
"""
import importlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
fai = importlib.import_module("FAI Server")

INI = ("[art_embed]\n"
       "embed_art_in_library = %s\n"
       "library_folders =\n")

failures = []


def check(name, ok, detail=""):
    print("%-58s %s" % (name, "PASS" if ok else "FAIL " + str(detail)))
    if not ok:
        failures.append(name)


# Pretend to be a frozen build whose EXE sits in `exe_dir`, with a bundled copy
# in `internal_dir`.
exe_dir = tempfile.mkdtemp(prefix="exedir_")
internal_dir = os.path.join(exe_dir, "_internal")
os.makedirs(internal_dir)

# The BUNDLED copy says OFF; the one the user edited BESIDE THE EXE says ON.
with open(os.path.join(internal_dir, "online_store.ini"), "w") as fh:
    fh.write(INI % "false")
with open(os.path.join(exe_dir, "online_store.ini"), "w") as fh:
    fh.write(INI % "true")

real_frozen = getattr(sys, "frozen", False)
real_exec = sys.executable
real_file = fai.__file__
try:
    sys.frozen = True
    sys.executable = os.path.join(exe_dir, "server.exe")
    fai.__file__ = os.path.join(internal_dir, "FAI Server.py")

    fai._art_embed_config()
    check("frozen: the user's copy beside the EXE wins over the bundled one",
          fai.ART_EMBED_ENABLED is True,
          "enabled=%r - the bundled copy was read instead" % fai.ART_EMBED_ENABLED)
finally:
    sys.frozen = real_frozen
    sys.executable = real_exec
    fai.__file__ = real_file

# Now flip it: beside the EXE says OFF, bundled says ON.
with open(os.path.join(exe_dir, "online_store.ini"), "w") as fh:
    fh.write(INI % "false")
with open(os.path.join(internal_dir, "online_store.ini"), "w") as fh:
    fh.write(INI % "true")

try:
    sys.frozen = True
    sys.executable = os.path.join(exe_dir, "server.exe")
    fai.__file__ = os.path.join(internal_dir, "FAI Server.py")
    fai._art_embed_config()
    check("frozen: turning it OFF beside the EXE is honoured",
          fai.ART_EMBED_ENABLED is False,
          "enabled=%r" % fai.ART_EMBED_ENABLED)
finally:
    sys.frozen = real_frozen
    sys.executable = real_exec
    fai.__file__ = real_file

# With no library_folders at all, this user's own Music folder is used.
music = os.path.join(os.path.expanduser("~"), "Music")
expected = [music] if os.path.isdir(music) else []
check("default library folder is this user's own Music folder",
      fai.ART_EMBED_FOLDERS == expected,
      "got %r, expected %r" % (fai.ART_EMBED_FOLDERS, expected))
if expected:
    check("  and it is under the real user profile",
          expected[0].lower().startswith(os.path.expanduser("~").lower()),
          expected)

# A literal C:\Users\User\Music must NOT be mistaken for a placeholder.
check("a literal C:\\Users\\User\\Music is not silently invented",
      not any("users\\user\\music" in p.lower() for p in fai.ART_EMBED_FOLDERS),
      fai.ART_EMBED_FOLDERS)

print()
if failures:
    print("FAILED: %d" % len(failures))
    for name in failures:
        print("  - %s" % name)
    sys.exit(1)
print("all art-embed config checks passed")
sys.exit(0)