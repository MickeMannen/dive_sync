"""What the About pages show: version, license, project links and the
versions of the parts dive_sync is built on. Shared by the desktop app and
the web dashboard."""
from __future__ import annotations

import os
import platform
import re
from importlib import metadata
from typing import Dict, List, Optional

APP_NAME = "DiveSync"
AUTHOR = "Mikael Christersson"
PROJECT_URL = "https://github.com/mickemannen/dive_sync"
LICENSE_NAME = "MIT"

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The license travels inside the app too (LICENSE is not always next to the
# code in a bundle); this is its text, kept in step with LICENSE.
_MIT_TEXT = """MIT License

Copyright (c) 2026 Mikael Christersson

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def app_version() -> str:
    """The running version: APP_VERSION (set by the Docker image build), the
    desktop bundle's own metadata, then pyproject.toml in a source checkout."""
    env = os.environ.get("APP_VERSION")
    if env and env != "local-dev":
        return env.lstrip("v")
    try:
        return metadata.version("desktop")
    except metadata.PackageNotFoundError:
        pass
    try:
        with open(os.path.join(_ROOT, "pyproject.toml"), encoding="utf-8") as f:
            m = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "local-dev"


def license_text() -> str:
    try:
        with open(os.path.join(_ROOT, "LICENSE"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return _MIT_TEXT


def _version_of(package: str) -> Optional[str]:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        pass
    try:                                    # e.g. PySide6 inside an app bundle
        module = __import__(package)
        return getattr(module, "__version__", None)
    except ImportError:
        return None


def components() -> List[Dict[str, str]]:
    """The main parts dive_sync runs on, with their versions where installed."""
    out = [{"name": "Python", "version": platform.python_version()}]
    for label, package in (("Qt for Python (PySide6)", "PySide6"), ("FastAPI", "fastapi"),
                           ("garminconnect", "garminconnect"), ("pydantic", "pydantic"), ("dulwich", "dulwich")):
        version = _version_of(package)
        if version:
            out.append({"name": label, "version": version})
    return out


def about_info() -> Dict[str, object]:
    return {
        "name": APP_NAME,
        "version": app_version(),
        "author": AUTHOR,
        "license_name": LICENSE_NAME,
        "license_text": license_text(),
        "project_url": PROJECT_URL,
        "releases_url": PROJECT_URL + "/releases",
        "issues_url": PROJECT_URL + "/issues",
        "components": components(),
        "platform": f"{platform.system()} {platform.release()}",
    }
