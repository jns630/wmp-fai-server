"""Confirm the frozen Win7 build actually contains both fixes.

PyInstaller compresses the app's bytecode, so grepping the EXE finds nothing.
This uses PyInstaller's own archive reader to pull the compiled "FAI Server"
module out of the frozen EXE and searches that.
"""
import os
import sys

from PyInstaller.archive.readers import CArchiveReader

repo = os.path.dirname(os.path.abspath(__file__))
exe = os.path.join(repo, "dist-win7", "WMP-FAI-Server-Win7Test",
                   "WMP-FAI-Server-Win7Test.exe")

if not os.path.isfile(exe):
    print("FAIL build not found: %s" % exe)
    sys.exit(1)

reader = CArchiveReader(exe)
names = list(reader.toc)
print("archive entries: %d" % len(names))

# Find the app module's compiled form.
candidates = [n for n in names if "FAI" in n or "artwork_embed" in n]
print("matching entries: %s" % candidates[:6])

blob = b""
for name in candidates:
    try:
        data = reader.extract(name)
        if isinstance(data, (bytes, bytearray)):
            blob += bytes(data)
    except Exception:
        pass

# The archive stores names in its own table too.
blob += "\n".join(names).encode("utf-8", "replace")

# Marshal string constants keep their characters, so look for the markers that
# only exist if the new code was compiled in.
markers = {
    "_itunes_art_hires": "iTunes artwork URL fix",
    "not identifiable as this album": "untagged-folder refusal guard",
    "_disambiguate": "ambiguity helper",
    "no album tags on disk yet": "untagged-fallback log line",
    "100x100bb": "iTunes size segment handling",
    # The disc discriminator. A rip has ?cd= and an EMPTY toc, so a guard that
    # tested `toc` alone let a rip be written to mid-write. This string only
    # exists once the guard accepts either identifier.
    "CD rip (cd/toc present)": "disc identified by cd OR toc, not toc alone",
    # The per-apply artwork token. `direct` mode - the DEFAULT, and the mode a
    # Windows 11 install runs - used to emit a byte-identical URL on every apply,
    # so WMP never re-fetched a cover for a collection. The parameter name and
    # both helpers are here only if that fix is in the build.
    "fai_v": "per-apply artwork token parameter",
    "_art_url_with_token": "per-apply token applied to the cover URL",
    "_strip_art_token": "per-apply token removed before fetching a provider",
    # The rip-artwork fix. A CD rip used to be REFUSED by the embed guard on the
    # claim that WMP writes the art itself; from a real rip it demonstrably does
    # not, so rips had an album picture and no track art. These three only exist
    # once the refusal became a deferred write.
    "_schedule_rip_art_embed": "rip cover write is deferred, not refused",
    "ART_EMBED_RIP_SETTLE": "a file WMP is still writing is skipped, not raced",
    "_embed_art_into_files": "shared worker for library albums and rips",
}

failed = False
print()
for marker, what in markers.items():
    present = marker.encode("utf-8", "replace") in blob
    print("%-36s %s  (%s)" % (marker, "FOUND" if present else "MISSING", what))
    if not present:
        failed = True

print()
if failed:
    print("FAILED: the frozen build is missing at least one fix; rebuild it")
    sys.exit(1)
print("frozen Win7 build contains both fixes")
sys.exit(0)