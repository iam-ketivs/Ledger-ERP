"""Check every file listed in MANIFEST.txt is present and unchanged.

Written because the two failures so far were both caused by a half-finished copy:
new templates sitting against an old app.py. This catches that in seconds.
"""
import hashlib, os, sys

here = os.path.dirname(os.path.abspath(__file__))
man = os.path.join(here, "MANIFEST.txt")
if not os.path.exists(man):
    print("MANIFEST.txt is missing - cannot verify."); sys.exit(1)

missing, changed, ok = [], [], 0
for line in open(man):
    parts = line.split(None, 2)
    if len(parts) != 3 or not parts[0].isalnum() or len(parts[0]) != 16:
        continue
    want, _size, rel = parts[0], parts[1], parts[2].strip()
    full = os.path.join(here, rel.replace("/", os.sep))
    if not os.path.exists(full):
        missing.append(rel); continue
    got = hashlib.sha256(open(full, "rb").read()).hexdigest()[:16]
    if got != want: changed.append(rel)
    else: ok += 1

print("=" * 62)
print("  LEDGER FILE CHECK")
print("=" * 62)
print(f"  {ok} files correct")
if missing:
    print(f"\n  {len(missing)} MISSING - this will break the app:")
    for m in missing: print("     -", m)
if changed:
    print(f"\n  {len(changed)} different from the original:")
    for m in changed: print("     ~", m)
    print("\n  (fine if you edited them on purpose; a problem if you did not)")
if not missing and not changed:
    print("\n  Everything matches. This is a complete, consistent copy.")
elif missing:
    print("\n  Copy the missing files from the master zip and try again.")
print("=" * 62)
