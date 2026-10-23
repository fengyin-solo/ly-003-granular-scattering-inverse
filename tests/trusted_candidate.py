#!/usr/bin/env python3
"""Run the candidate as nobody while preserving private verifier ownership."""

from __future__ import annotations

import os
import sys
from pathlib import Path


SETPRIV = Path("/usr/bin/setpriv")
TRUSTED_PYTHON = Path("/usr/local/bin/python3")
NOBODY_UID = "65534"
NOBODY_GID = "65534"


def allow_runtime_path(raw: str) -> None:
    if os.geteuid() != 0:
        return
    path = Path(raw)
    if not path.is_absolute():
        return
    root = Path(os.environ.get("TASK_VERIFIER_TMP", "/tmp")).resolve()
    target = path if path.exists() else path.parent
    try:
        resolved = target.resolve()
    except OSError:
        return
    if resolved != root and root not in resolved.parents:
        return
    current = resolved
    while current == root or root in current.parents:
        try:
            current.chmod(current.stat().st_mode | 0o007)
        except OSError:
            break
        if current == root:
            break
        current = current.parent
    if target.is_dir():
        for item in target.rglob("*"):
            try:
                mode = item.stat().st_mode
                item.chmod(mode | (0o007 if item.is_dir() else 0o006))
            except OSError:
                pass
    elif target.exists():
        try:
            target.chmod(target.stat().st_mode | 0o006)
        except OSError:
            pass


def main() -> int:
    candidate = os.environ.get("TASK_CANDIDATE", "")
    if not candidate:
        sys.stderr.write("TASK_CANDIDATE is missing\n")
        return 127
    args = sys.argv[1:]
    for value in args:
        allow_runtime_path(value)
    if os.geteuid() == 0 and SETPRIV.is_file() and TRUSTED_PYTHON.is_file():
        os.execv(
            str(SETPRIV),
            [
                str(SETPRIV),
                "--reuid",
                NOBODY_UID,
                "--regid",
                NOBODY_GID,
                "--clear-groups",
                str(TRUSTED_PYTHON),
                candidate,
                *args,
            ],
        )
    os.execv(sys.executable, [sys.executable, candidate, *args])
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
