"""Runs the eMule workspace command-line interface."""

from __future__ import annotations

import subprocess
import sys

from .python_runtime import main

if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Workspace: {error}", file=sys.stderr)
        raise SystemExit(1) from error
