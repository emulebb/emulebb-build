"""Keep Git hook repository bindings out of cross-checkout child commands."""
from __future__ import annotations

from functools import lru_cache
import os
import subprocess


@lru_cache(maxsize=1)
def _git_local_environment_variables() -> tuple[str, ...]:
    """Ask Git which inherited variables bind a command to the hook repository."""
    with subprocess.Popen(["git", "rev-parse", "--local-env-vars"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as process:
        stdout, stderr = process.communicate()
        if process.returncode:
            raise RuntimeError(f"Cannot identify repository-local Git environment: {stderr.strip()}")
    return tuple(stdout.splitlines())


def git_environment() -> dict[str, str]:
    """Copy operator configuration without a hook's repository or index binding."""
    environment = os.environ.copy()
    # WHY: Git exports repository-local variables into hooks. A child Git init
    # or even a -C query otherwise targets that original repository's metadata.
    # Retain global/system configuration and authentication outside Git's list.
    for name in _git_local_environment_variables():
        environment.pop(name, None)
    return environment
