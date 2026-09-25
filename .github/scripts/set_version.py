"""Write the release tag into pyproject.toml's version before a release build,
so the tag is the one place a release's version is set. Reads RELEASE_TAG
("v1.2.0" or "1.2.0"). Only X.Y.Z is accepted: Windows MSI installers need a
purely numeric version. The change is never committed - it only lives on
the build runner."""
import os
import pathlib
import re
import sys

tag = os.environ.get("RELEASE_TAG", "").strip()
version = tag[1:] if tag.startswith("v") else tag
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    sys.exit(f"::error::Release tag {tag!r} is not vX.Y.Z (e.g. v1.2.0)")

path = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"
text, count = re.subn(r'^version\s*=\s*"[^"]*"', f'version = "{version}"',
                      path.read_text(encoding="utf-8"), count=1, flags=re.M)
if count != 1:
    sys.exit("::error::No version line found in pyproject.toml")
path.write_text(text, encoding="utf-8")
print(f"pyproject.toml version set to {version}")
