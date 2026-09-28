"""Tests for ``./gilbert.sh stop`` and its PID file handling.

``stop`` used to read ``.gilbert/gilbert.pid`` and signal whatever it
named, but ``start`` never wrote that file, so it printed "No PID file
found" and left Gilbert running. There are two PID files now:
``.gilbert/supervisor.pid``, written by ``gilbert.sh``, and
``.gilbert/gilbert.pid``, written by Gilbert itself.

These tests drive the real script against a throwaway copy of the
checkout, so nothing here can signal a live Gilbert: every process the
script is allowed to find by command line has to have its working
directory set to the temporary directory.
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "gilbert.sh"

# ``stop`` polls once a second, so give it a little room on a loaded box.
WAIT_TIMEOUT = 15.0


@pytest.fixture
def fake_checkout(tmp_path: Path) -> Path:
    """A directory holding a copy of gilbert.sh and an empty .gilbert/."""
    shutil.copy2(SCRIPT, tmp_path / "gilbert.sh")
    (tmp_path / ".gilbert").mkdir()
    return tmp_path


def run_stop(checkout: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "./gilbert.sh", "stop"],
        cwd=checkout,
        capture_output=True,
        text=True,
        timeout=60,
    )


def spawn_sleeper(checkout: Path, argv0: str | None = None) -> subprocess.Popen[bytes]:
    """Start a long-lived process in ``checkout`` that dies on SIGTERM.

    ``argv0`` renames the process so it matches the command line pattern
    the script uses to find an orphaned Gilbert.
    """
    if argv0 is None:
        command = ["sleep", "300"]
    else:
        command = ["bash", "-c", f'exec -a "{argv0}" sleep 300']
    proc = subprocess.Popen(command, cwd=checkout)
    return proc


def wait_for_exit(proc: subprocess.Popen[bytes]) -> int | None:
    try:
        return proc.wait(timeout=WAIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None


def test_stop_terminates_the_supervisor_named_in_its_pid_file(
    fake_checkout: Path,
) -> None:
    pid_file = fake_checkout / ".gilbert" / "supervisor.pid"
    proc = spawn_sleeper(fake_checkout)
    pid_file.write_text(f"{proc.pid}\n")

    result = run_stop(fake_checkout)

    assert wait_for_exit(proc) is not None, "stop left the process running"
    assert result.returncode == 0, result.stderr
    assert "Gilbert stopped." in result.stdout
    assert not pid_file.exists(), "stop left the PID file behind"


def test_stop_removes_a_stale_pid_file(fake_checkout: Path) -> None:
    pid_file = fake_checkout / ".gilbert" / "supervisor.pid"
    # A PID that cannot be running: above the kernel's default pid_max.
    pid_file.write_text("99999999\n")

    result = run_stop(fake_checkout)

    assert result.returncode == 0, result.stderr
    assert "Removing stale supervisor PID file" in result.stdout
    assert "Gilbert is not running." in result.stdout
    assert not pid_file.exists()


def test_stop_reports_when_nothing_is_running(fake_checkout: Path) -> None:
    result = run_stop(fake_checkout)

    assert result.returncode == 0, result.stderr
    assert "Gilbert is not running." in result.stdout


def test_stop_terminates_an_orphaned_gilbert_without_a_pid_file(
    fake_checkout: Path,
) -> None:
    """A supervisor that died first leaves the python process orphaned.

    ``stop`` has to find it by command line and working directory,
    because there is no PID file naming it.
    """
    proc = spawn_sleeper(fake_checkout, argv0="python3 -m gilbert")
    # Give bash time to exec the renamed process before pgrep looks.
    time.sleep(0.5)

    result = run_stop(fake_checkout)

    assert wait_for_exit(proc) is not None, "stop left the orphan running"
    assert result.returncode == 0, result.stderr
    assert "Gilbert stopped." in result.stdout


def test_stop_lets_the_supervisor_forward_the_signal(fake_checkout: Path) -> None:
    """A supervisor named by the PID file passes the stop on itself.

    Gilbert must receive exactly one signal: a second one turns its
    graceful shutdown into a forced quit, which is how the database got
    corrupted. So when a supervisor is there to forward the stop, ``stop``
    signals only the supervisor.
    """
    app = spawn_sleeper(fake_checkout, argv0="python3 -m gilbert")
    supervisor_script = fake_checkout / "fake-supervisor.sh"
    supervisor_script.write_text(
        "#!/usr/bin/env bash\n"
        f"trap 'kill -TERM {app.pid} 2>/dev/null; exit 0' TERM\n"
        "while true; do sleep 0.2; done\n"
    )
    supervisor_script.chmod(0o755)
    supervisor = subprocess.Popen(["bash", str(supervisor_script)], cwd=fake_checkout)
    (fake_checkout / ".gilbert" / "supervisor.pid").write_text(f"{supervisor.pid}\n")
    time.sleep(0.5)

    result = run_stop(fake_checkout)

    assert "forwards the stop to Gilbert" in result.stdout, result.stdout
    assert "signalled Gilbert directly" not in result.stdout
    assert wait_for_exit(app) is not None, "the supervisor did not forward the stop"
    assert wait_for_exit(supervisor) is not None
    assert result.returncode == 0, result.stderr


def test_stop_signals_gilbert_directly_without_a_pid_file(
    fake_checkout: Path,
) -> None:
    """An older supervisor does not forward, so Gilbert is signalled.

    ``stop`` also has to take that supervisor down, or it reads Gilbert's
    exit as a crash and relaunches it 20 seconds later.
    """
    app = spawn_sleeper(fake_checkout, argv0="python3 -m gilbert")
    supervisor = spawn_sleeper(fake_checkout, argv0="bash ./gilbert.sh start")
    time.sleep(0.5)

    result = run_stop(fake_checkout)

    assert "signalled Gilbert directly" in result.stdout, result.stdout
    assert wait_for_exit(app) is not None, "stop left Gilbert running"
    assert wait_for_exit(supervisor) is not None, "stop left the supervisor running"
    assert result.returncode == 0, result.stderr


def test_stop_uses_gilberts_own_pid_file(fake_checkout: Path, tmp_path: Path) -> None:
    """Gilbert writes ``.gilbert/gilbert.pid`` itself, and ``stop`` reads it.

    The process here runs outside the checkout, so only the PID file can
    account for it being found.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    app = spawn_sleeper(elsewhere, argv0="python3 -m gilbert")
    (fake_checkout / ".gilbert" / "gilbert.pid").write_text(f"{app.pid}")
    time.sleep(0.5)

    result = run_stop(fake_checkout)

    assert wait_for_exit(app) is not None, "stop ignored Gilbert's own PID file"
    assert result.returncode == 0, result.stderr
    assert "Gilbert stopped." in result.stdout


def test_stop_ignores_the_uv_wrapper(fake_checkout: Path) -> None:
    """``uv run python -m gilbert`` matches on name but must not be signalled.

    uv passes a signal of its own on to Gilbert, so signalling both is the
    double signal that forces the shutdown.
    """
    wrapper = spawn_sleeper(fake_checkout, argv0="uv run python -m gilbert")
    time.sleep(0.5)
    try:
        result = run_stop(fake_checkout)

        assert result.returncode == 0, result.stderr
        assert "Gilbert is not running." in result.stdout
        assert wrapper.poll() is None, "stop signalled the uv wrapper"
    finally:
        wrapper.send_signal(signal.SIGKILL)
        wrapper.wait(timeout=WAIT_TIMEOUT)


def test_stop_ignores_a_gilbert_in_a_different_checkout(
    fake_checkout: Path, tmp_path: Path
) -> None:
    """Two installations on one machine must not stop each other."""
    other = tmp_path / "other-checkout"
    other.mkdir()
    proc = spawn_sleeper(other, argv0="python3 -m gilbert")
    time.sleep(0.5)
    try:
        result = run_stop(fake_checkout)

        assert result.returncode == 0, result.stderr
        assert "Gilbert is not running." in result.stdout
        assert proc.poll() is None, "stop killed another checkout's Gilbert"
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=WAIT_TIMEOUT)
