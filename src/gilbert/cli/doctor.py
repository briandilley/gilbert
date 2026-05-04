"""``gilbert doctor`` — sanity-check every plugin's runtime dependencies.

Boots only as much of Gilbert as it takes to discover plugins (no
service startup, no network), instantiates each ``Plugin`` so it can
declare its ``runtime_dependencies()``, and runs each declared
``check_cmd`` via ``/bin/sh -c``. Prints a PASS/FAIL line per check.

Exit codes:

- ``0`` — every check passed.
- ``1`` — at least one check failed.

Flags:

- ``--install`` — for any failing check whose plugin declared an
  ``auto_install_cmd``, run that command and re-check. Useful for
  installing things like Playwright's Chromium binary that live in
  user-scoped caches and don't need sudo. Apt/Brew-style installs
  do NOT get an ``auto_install_cmd`` so they're surfaced as install
  hints only.
- ``--plugin <name>`` — restrict to a single plugin. Repeatable.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from typing import Any

from gilbert.config import _load_yaml
from gilbert.interfaces.plugin import Plugin, RuntimeDependency
from gilbert.plugins.loader import PluginLoader, PluginManifest

_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{_RESET}"


def _discover_plugins(only: set[str] | None = None) -> list[tuple[PluginManifest, Plugin]]:
    """Load enough config to find plugin directories, then instantiate
    every Plugin without booting Gilbert. Returns ``(manifest, plugin)``
    tuples for plugins matching ``only`` (or all when ``None``)."""
    from gilbert.config import DEFAULT_CONFIG_PATH, OVERRIDE_CONFIG_PATH, _deep_merge

    base: dict[str, Any] = {}
    if DEFAULT_CONFIG_PATH.exists():
        base = _load_yaml(DEFAULT_CONFIG_PATH)
    overrides: dict[str, Any] = {}
    if OVERRIDE_CONFIG_PATH.exists():
        overrides = _load_yaml(OVERRIDE_CONFIG_PATH)
    merged = _deep_merge(base, overrides)
    plugins_raw = merged.get("plugins", {}) or {}
    directories = (
        plugins_raw.get("directories", []) if isinstance(plugins_raw, dict) else []
    )

    loader = PluginLoader(cache_dir=plugins_raw.get("cache_dir", ".gilbert/plugin-cache"))
    manifests = loader.scan_directories(directories)

    out: list[tuple[PluginManifest, Plugin]] = []
    for manifest in manifests:
        meta_name = manifest.to_plugin_meta().name
        if only is not None and meta_name not in only:
            continue
        try:
            plugin = loader.load_from_manifest(manifest)
        except Exception as exc:
            print(
                _color(f"  ⚠ {meta_name}: failed to load — {exc}", _YELLOW),
                file=sys.stderr,
            )
            continue
        out.append((manifest, plugin))
    return out


def _run_check(cmd: str) -> bool:
    """Run a shell command via ``/bin/sh -c``; return True on exit 0."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/sh",
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _run_install(cmd: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/sh",
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False, "timed out after 10 minutes"
    out = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, out.strip()[-2000:]


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="gilbert doctor",
        description="Sanity-check every plugin's runtime dependencies.",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help=(
            "Auto-run each failing check's ``auto_install_cmd`` if the "
            "plugin declared one. Skips checks without an auto-install."
        ),
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        help="Limit to a specific plugin (repeatable).",
    )
    args = parser.parse_args()

    only = set(args.plugin) if args.plugin else None

    plugins = _discover_plugins(only=only)
    if not plugins:
        print(
            _color(
                "No plugins discovered. Did you forget to init the std-plugins submodule?",
                _YELLOW,
            )
        )
        return 0

    total_fail = 0

    for _manifest, plugin in plugins:
        deps: list[RuntimeDependency]
        try:
            deps = plugin.runtime_dependencies()
        except Exception as exc:
            print(
                _color(
                    f"  ⚠ {plugin.metadata().name}: runtime_dependencies() raised — {exc}",
                    _YELLOW,
                )
            )
            continue
        if not deps:
            continue

        print(_color(f"\n{plugin.metadata().name}", _BOLD))
        for dep in deps:
            ok = _run_check(dep.check_cmd)
            if ok:
                print(f"  {_color('PASS', _GREEN)}  {dep.name} — {dep.description}")
                continue

            # Failing check.
            if args.install and dep.auto_install_cmd:
                print(
                    f"  {_color('FAIL', _RED)}  {dep.name} — {dep.description}"
                )
                print(f"        installing: {dep.auto_install_cmd}")
                installed_ok, log = _run_install(dep.auto_install_cmd)
                if installed_ok and _run_check(dep.check_cmd):
                    print(f"        {_color('OK', _GREEN)} (installed and verified)")
                    continue
                print(_color("        install failed", _RED))
                if log:
                    print(_indent(log, 8))
                total_fail += 1
                print(f"        hint: {dep.install_hint}")
            else:
                print(f"  {_color('FAIL', _RED)}  {dep.name} — {dep.description}")
                print(f"        hint: {dep.install_hint}")
                total_fail += 1

    if total_fail:
        print(
            _color(
                f"\n{total_fail} check(s) failed. Re-run with `--install` to auto-fix where supported.",
                _YELLOW,
            )
        )
        return 1
    print(_color("\nAll checks passed.", _GREEN))
    return 0


def _indent(s: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line for line in s.splitlines())


if __name__ == "__main__":
    sys.exit(main())


# Quiet unused-import pruner if someone drops shlex by mistake.
_ = shlex
