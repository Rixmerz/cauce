from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cauce import cli, interpreter, link

ROOT = Path(__file__).resolve().parents[1]


def test_newer_python_prefers_the_newest_named_one_and_respects_a_choice():
    seen = []

    def which(name, path=None):
        seen.append(name)
        return f"/usr/bin/{name}" if name in ("python3.12", "python3.11") else None

    assert interpreter.newer_python({"PATH": "/usr/bin"}, which) == "/usr/bin/python3.12"
    assert seen[:2] == ["python3.14", "python3.13"]
    assert interpreter.newer_python({"CAUCE_PYTHON": "python3"}, which) is None
    assert interpreter.newer_python({"CAUCE_REEXEC": "1"}, which) is None
    assert interpreter.newer_python({}, lambda name, path=None: None) is None


def test_the_reexec_marker_is_never_inherited(tmp_path):
    """A worker's hooks start on the old python3 again; they must be free to look."""
    code = ("import os, runpy, sys\n"
            "os.environ['CAUCE_REEXEC'] = '1'\n"
            "sys.argv = ['cauce', 'matrix']\n"
            "try:\n    runpy.run_module('cauce', run_name='__main__')\n"
            "except SystemExit:\n    pass\n"
            "print('marker', os.environ.get('CAUCE_REEXEC'))\n")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False,
                          env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert proc.stdout.strip().endswith("marker None")


def _install(config: Path, version: str, mtime: float) -> Path:
    exe = config / "plugins" / "cache" / "rixmerz" / "cauce" / version / "bin" / "cauce"
    exe.parent.mkdir(parents=True)
    exe.write_text(f"#!/bin/sh\necho {version} \"$@\"\n")
    exe.chmod(0o755)
    os.utime(exe, (mtime, mtime))
    return exe


def test_the_shim_runs_the_newest_install_and_survives_an_update(tmp_path):
    config = tmp_path / "claude"
    _install(config, "0.1.0", 1_000)
    shim = link.write(tmp_path / "bin", tmp_path / "gone" / "cauce")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(config)}
    run = lambda: subprocess.run([str(shim), "board"], capture_output=True, text=True, env=env, check=False)  # noqa: E731
    assert run().stdout.strip() == "0.1.0 board"
    _install(config, "0.2.0", 2_000)  # a plugin update: a new version directory
    assert run().stdout.strip() == "0.2.0 board"


def test_the_shim_falls_back_to_the_launcher_it_was_written_from(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "none")}
    shim = link.write(tmp_path / "bin", ROOT / "bin" / "cauce")
    proc = subprocess.run([str(shim), "matrix"], capture_output=True, text=True, check=False,
                          env={**env, "CAUCE_HOME": str(tmp_path / "h")})
    assert proc.returncode == 0 and "debug-unclear" in proc.stdout
    shim = link.write(tmp_path / "bin", tmp_path / "it's gone" / "cauce")  # rewriting its own shim is fine
    proc = subprocess.run([str(shim)], capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 127 and "claude plugin install cauce@rixmerz" in proc.stderr


def test_link_never_replaces_a_cauce_it_did_not_write(tmp_path):
    target = tmp_path / "bin"
    target.mkdir()
    (target / "cauce").write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(FileExistsError):
        link.write(target, ROOT / "bin" / "cauce")
    assert link.remove(target) is None and (target / "cauce").exists()
    (target / "cauce").unlink()
    (target / "cauce").symlink_to(ROOT / "bin" / "cauce")
    with pytest.raises(FileExistsError):
        link.write(target, ROOT / "bin" / "cauce")


def test_cauce_link_command(tmp_path, capsys, monkeypatch):
    target = tmp_path / "bin"
    monkeypatch.setenv("PATH", "/usr/bin")
    assert cli.main(["link", "--dir", str(target)]) == 0
    out = capsys.readouterr().out
    assert f"wrote {target / 'cauce'}" in out and "is not on PATH" in out
    assert link.MARKER in (target / "cauce").read_text()
    monkeypatch.setenv("PATH", f"{target}:/usr/bin")
    assert cli.main(["link", "--dir", str(target)]) == 0
    assert "not on PATH" not in capsys.readouterr().out
    assert cli.main(["link", "--dir", str(target), "--remove"]) == 0
    assert "removed" in capsys.readouterr().out and not (target / "cauce").exists()
    assert cli.main(["link", "--dir", str(target), "--remove"]) == 0
    assert "no `cauce link` shim" in capsys.readouterr().out
    (target / "cauce").write_text("someone else's")
    assert cli.main(["link", "--dir", str(target)]) == 1
    assert "was not written by `cauce link`" in capsys.readouterr().err
    monkeypatch.setenv("HOME", str(tmp_path))
    assert link.default_dir(os.environ) == tmp_path / ".local" / "bin"


def _plugin(config: Path, version: str, *, executable: bool = True, manifest: str | None = None) -> Path:
    root = config / "plugins" / "cache" / "rixmerz" / "cauce" / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(manifest or f'{{"name": "cauce", "version": "{version}"}}')
    launcher = root / "bin" / "cauce"
    launcher.parent.mkdir()
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755 if executable else 0o644)
    return launcher


def test_installs_are_ordered_by_the_version_they_say_not_by_their_dates(tmp_path):
    config = tmp_path / "config"
    newest = _plugin(config, "0.10.0")
    _plugin(config, "0.9.3")
    _plugin(config, "0.11.0", executable=False)
    _plugin(config, "broken", manifest="not json")
    _plugin(config, "odd", manifest='{"version": "next"}')
    found = link.installed({"CLAUDE_CONFIG_DIR": str(config)})
    assert found == [((0, 9, 3), found[0][1]), ((0, 10, 0), newest)]
    assert link.installed({"HOME": str(tmp_path)}) == []
    assert link.parse_version("1.2") == (1, 2) and link.parse_version("1.x") is None
