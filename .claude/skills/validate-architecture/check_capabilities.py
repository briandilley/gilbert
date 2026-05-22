#!/usr/bin/env python3
"""Audit consumer-side capability lookups against declared capabilities.

Every name passed to ``resolver.get_capability("...")`` /
``resolver.require_capability("...")`` / ``resolver.get_all("...")``
must appear in some service's ``ServiceInfo.capabilities=frozenset({...})``.
A consumer asking for ``"ai"`` when the service advertises ``"ai_chat"``
silently returns ``None`` — the calling code's ``isinstance`` gate falls
through to "service unavailable" and the feature only breaks when the
dead code path runs.

Prints ``OK`` and exits 0 when every consumed capability has at least
one declarer. Otherwise prints each undeclared capability with the
file:line of every consumer and exits 1.

Lookups through a variable (``get_capability(self._cap_name)``) can't
be checked statically — they're skipped.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_DECL = re.compile(r"capabilities=frozenset\(\s*\{([^}]*)\}", re.DOTALL)
_CONSUME = re.compile(
    r'(?:get_capability|require_capability|get_all)\(\s*["\']([^"\']+)["\']\s*\)'
)
_QUOTED = re.compile(r"""["']([^"']+)["']""")

_ROOTS = ("src/gilbert", "std-plugins", "local-plugins", "installed-plugins")
_SKIP_DIRS = frozenset({".venv", "venv", "node_modules", "__pycache__", ".git"})


def main() -> int:
    repo_root = Path(__file__).resolve().parents[3]
    declared: set[str] = set()
    consumed: dict[str, list[tuple[str, int]]] = {}

    for root in _ROOTS:
        base = repo_root / root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            try:
                text = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            for m in _DECL.finditer(text):
                for q in _QUOTED.finditer(m.group(1)):
                    declared.add(q.group(1))
            for m in _CONSUME.finditer(text):
                line = text[: m.start()].count("\n") + 1
                rel = path.relative_to(repo_root)
                consumed.setdefault(m.group(1), []).append((str(rel), line))

    missing = sorted(set(consumed) - declared)
    if not missing:
        print("OK: every consumed capability is declared somewhere.")
        return 0

    for cap in missing:
        print(f"undeclared capability: {cap!r}")
        for f, ln in consumed[cap]:
            print(f"    consumed at {f}:{ln}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
