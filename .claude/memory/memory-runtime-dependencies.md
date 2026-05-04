# Plugin runtime_dependencies + gilbert doctor

## Summary
``Plugin.runtime_dependencies()`` lets a plugin declare non-pip OS deps (browser binaries, system packages, ffmpeg, tesseract, …). ``./gilbert.sh doctor`` (or ``uv run python -m gilbert.cli.doctor``) iterates every loaded plugin's deps, runs each declared ``check_cmd`` via ``/bin/sh -c``, and prints PASS/FAIL with the install hint on failure. ``--install`` runs ``auto_install_cmd`` for plugins that opted in (reserved for unattended-safe paths only — apt-get/sudo installs stay manual).

## Details

### Interface
``src/gilbert/interfaces/plugin.py``:

```python
@dataclass(frozen=True)
class RuntimeDependency:
    name: str
    description: str
    check_cmd: str            # /bin/sh -c
    install_hint: str          # always shown on failure
    auto_install_cmd: str = ""  # empty → no auto-install; use --install to run

class Plugin(ABC):
    def runtime_dependencies(self) -> list[RuntimeDependency]:
        return []
```

Plugins override on the Plugin subclass (not on a Service) — the doctor doesn't need the service to be running, just the plugin's metadata.

### Doctor CLI (`src/gilbert/cli/doctor.py`)
- Boots only the plugin loader (``PluginLoader.scan_directories`` + ``load_from_manifest``); does NOT call ``Plugin.setup()`` or start any service. So the doctor is fast and side-effect-free.
- Runs ``check_cmd`` via ``subprocess.run(shell=True, executable="/bin/sh", capture_output=True, timeout=60)``. Exit 0 = PASS.
- ``--install`` flag runs ``auto_install_cmd`` (10-minute timeout). Distinguishes:
  - **Install command failed** (exit ≠ 0) → print captured output + the install_hint.
  - **Install ran but the check still fails** → print last 800 chars of install output + the install_hint. Catches the case where the auto-install does its part but a sibling dep (e.g. OS shared libs the auto-installer can't touch) still needs manual work.
- ``--plugin <name>`` flag (repeatable) limits to specific plugins.

### Check design
The check should ideally exercise the dep, not just probe its file path. The browser plugin learned this the hard way:
- Probing ``playwright.chromium.executable_path`` passed even when ``launch(headless=True)`` failed because Playwright >= 1.49 uses a separate ``chromium-headless-shell`` binary that the path probe doesn't see.
- Trying to ``ldconfig -p | grep -q libnss3.so`` produced false negatives because ``ldconfig`` lives in ``/sbin/`` (not on a regular user's PATH on Debian).

Both problems disappeared by switching to a real ``chromium.launch(headless=True)`` exercise: missing binary, missing OS lib, sandbox failure — all surface as one failure.

### auto_install_cmd guidance
Use it only for things that:
- Don't need sudo (e.g. ``playwright install chromium`` writes to the per-user cache).
- Don't prompt interactively.
- Don't surprise the user (network downloads ≤ a few hundred MB).

Skip it for: apt-get / dnf / brew / chocolatey installs (need sudo or interactive elevation), Docker engine installs (varies wildly per platform), kernel modules. Surface those in ``install_hint`` as a copy/paste-ready command.

### Examples
- Browser: ``docker info`` (preferred) + headless-Chromium-launch (fallback). Auto-install fetches both Playwright binaries; OS-lib install hint points at Playwright's docs.
- Tesseract: ``tesseract --version``. No auto-install — apt/dnf/brew per-OS.

### Adding to a plugin
1. Override ``Plugin.runtime_dependencies()`` returning a list of ``RuntimeDependency``.
2. Make ``check_cmd`` exit 0 only when the dep is genuinely usable.
3. Write ``install_hint`` so a user can copy/paste a single command (or follow a single doc link) per OS.

### Backwards compat
The default ``[]`` means existing plugins don't need any change. ``./gilbert.sh doctor`` shows nothing for plugins that don't declare deps.

## Related
- ``src/gilbert/interfaces/plugin.py`` (RuntimeDependency, Plugin.runtime_dependencies)
- ``src/gilbert/cli/doctor.py``
- ``gilbert.sh`` (``doctor`` subcommand)
- ``std-plugins/browser/plugin.py`` (canonical example: docker primary + chromium fallback)
- ``std-plugins/tesseract/plugin.py`` (canonical example: simple binary check, no auto-install)
- ``std-plugins/CLAUDE.md`` "Runtime dependencies" section
