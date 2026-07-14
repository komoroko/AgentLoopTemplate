"""agentloop — the Human-on-the-Loop development harness as an installable package.

Installed as a CLI (`agentloop <verb>`, see cli.py); product repositories keep only their
state (.agentloop/ and docs/) — the machinery lives here.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agentloop")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "0.0.0+source"
