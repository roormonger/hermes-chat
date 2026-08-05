"""Dependency check + optional self-install for the hermes-chat plugin.

Hermes Chat runs inside the same Python interpreter that Hermes uses, so its
runtime packages (FastAPI, uvicorn, pydantic, PyYAML) are expected to already be
present in that environment. If they are not, this module can install them from
`requirements.txt` so the plugin can start cleanly.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .config import _plugin_dir


REQUIRED_PACKAGES = [
    ("fastapi", "fastapi>=0.111"),
    ("uvicorn", "uvicorn[standard]>=0.29"),
    ("pydantic", "pydantic>=2.6"),
    ("yaml", "pyyaml>=6.0"),
    ("jwt", "pyjwt>=2.8"),
    ("bcrypt", "bcrypt>=4.1"),
]

OPTIONAL_PACKAGES = [
    ("edge_tts", "edge-tts>=6.1", "Voice output fallback (text-to-speech)"),
    ("faster_whisper", "faster-whisper>=1.0", "Voice input fallback (speech-to-text)"),
    ("imageio_ffmpeg", "imageio-ffmpeg>=0.5", "Audio conversion (ffmpeg)"),
    ("langdetect", "langdetect>=1.0", "Automatic language detection"),
]


def check_dependencies() -> list[str]:
    """Return a list of missing requirement spec strings.

    Import-only packages are checked by module name; the returned strings are the
    pip requirements needed to satisfy them.
    """
    missing: list[str] = []
    for module, requirement in REQUIRED_PACKAGES:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(requirement)
    return missing


def check_optional_dependencies() -> list[dict]:
    """Return a list of missing optional packages with metadata.

    Each entry is {"requirement": str, "feature": str}.
    """
    missing: list[dict] = []
    for module, requirement, feature in OPTIONAL_PACKAGES:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append({"requirement": requirement, "feature": feature})
    return missing


def requirements_file() -> Optional[Path]:
    """Return the path to the plugin's requirements.txt if it exists."""
    path = _plugin_dir() / "requirements.txt"
    return path if path.exists() else None


def install_dependencies(auto: bool = False) -> dict:
    """Install missing dependencies into the current Python environment.

    If `auto` is False, the function returns a plan without executing it unless
    all packages are already present. Set `auto=True` to actually run pip.
    """
    missing = check_dependencies()
    missing_optional = check_optional_dependencies()

    if not missing and not missing_optional:
        return {"status": "ok", "installed": [], "message": "All dependencies are already present."}

    req_file = requirements_file()
    if req_file is None:
        return {
            "status": "error",
            "installed": [],
            "message": "Could not locate requirements.txt in the plugin directory.",
        }

    if not auto:
        all_missing = missing + [d["requirement"] for d in missing_optional]
        return {
            "status": "pending",
            "missing": all_missing,
            "message": f"Run with auto=True to install: {', '.join(all_missing)}",
        }

    cmd = [sys.executable, "-m", "pip", "install", "-r", str(req_file)]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        )
        if result.returncode != 0:
            return {
                "status": "error",
                "installed": missing + [d["requirement"] for d in missing_optional],
                "message": result.stderr or result.stdout or "pip install failed",
                "command": " ".join(cmd),
            }
        return {
            "status": "installed",
            "installed": missing + [d["requirement"] for d in missing_optional],
            "message": result.stdout.strip() or "Dependencies installed successfully.",
            "command": " ".join(cmd),
        }
    except Exception as exc:  # pragma: no cover - environment issues only
        return {
            "status": "error",
            "installed": missing + [d["requirement"] for d in missing_optional],
            "message": str(exc),
            "command": " ".join(cmd),
        }
