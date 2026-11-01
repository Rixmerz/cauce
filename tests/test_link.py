from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from locks import cli, interpreter, link

ROOT = Path(__file__).resolve().parents[1]


def test_newer_python_prefers_the_newest_named_one_and_respects_a_choice():
    seen = []

    def which(name, path=None):
        seen.append(name)
        return f"/usr/bin/{name}" if name in ("python3.12", "python3.11") else None

    assert interpreter.newer_python({"PATH": "/usr/bin"}, which) == "/usr/bin/python3.12"
    assert seen[:2] == ["python3.14", "python3.13"]
    assert interpreter.newer_python({"LOCKS_PYTHON": "python3"}, which) is None
    assert interpreter.newer_python({"LOCKS_REEXEC": "1"}, which) is None
    assert interpreter.newer_python({}, lambda name, path=None: None) is None


def test_the_reexec_marker_is_never_inherited(tmp_path):
    """A worker's hooks start on the old python3 again; they must be free to look."""
    code = ("import os, runpy, sys\n"
            "os.environ['LOCKS_REEXEC'] = '1'\n"
            "sys.argv = ['locks', 'matrix']\n"
            "try:\n    runpy.run_module('locks', run_name='__main__')\n"
            "except SystemExit:\n    pass\n"
            "print('marker', os.environ.get('LOCKS_REEXEC'))\n")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False,
                          env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert proc.stdout.strip().endswith("marker None")


def _install(config: Path, version: str, mtime: float) -> Path:
    exe = config / "plugins" / "cache" / "rixmerz" / "locks" / version / "bin" / "locks"
    exe.parent.mkdir(parents=True)
    exe.write_text(f"#!/bin/sh\necho {version} \"$@\"\n")
    exe.chmod(0o755)
    os.utime(exe, (mtime, mtime))
    return exe


def test_the_shim_runs_the_newest_install_and_survives_an_update(tmp_path):
    config = tmp_path / "claude"
    _install(config, "0.1.0", 1_000)
    shim = link.write(tmp_path / "bin", tmp_path / "gone" / "locks")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(config)}
    run = lambda: subprocess.run([str(shim), "board"], capture_output=True, text=True, env=env, check=False)  # noqa: E731
    assert run().stdout.strip() == "0.1.0 board"
    _install(config, "0.2.0", 2_000)  # a plugin update: a new version directory
    assert run().stdout.strip() == "0.2.0 board"


def test_the_shim_falls_back_to_the_launcher_it_was_written_from(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "none")}
    shim = link.write(tmp_path / "bin", ROOT / "bin" / "locks")
    proc = subprocess.run([str(shim), "matrix"], capture_output=True, text=True, check=False,
                          env={**env, "LOCKS_HOME": str(tmp_path / "h")})
    assert proc.returncode == 0 and "debug-unclear" in proc.stdout
    shim = link.write(tmp_path / "bin", tmp_path / "it's gone" / "locks")  # rewriting its own shim is fine
    proc = subprocess.run([str(shim)], capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 127 and "claude plugin install locks@rixmerz" in proc.stderr


def test_link_never_replaces_a_locks_it_did_not_write(tmp_path):
    target = tmp_path / "bin"
    target.mkdir()
    (target / "locks").write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(FileExistsError):
        link.write(target, ROOT / "bin" / "locks")
    assert link.remove(target) is None and (target / "locks").exists()
    (target / "locks").unlink()
    (target / "locks").symlink_to(ROOT / "bin" / "locks")
    with pytest.raises(FileExistsError):
        link.write(target, ROOT / "bin" / "locks")


def test_locks_link_command(tmp_path, capsys, monkeypatch):
    target = tmp_path / "bin"
    monkeypatch.setenv("PATH", "/usr/bin")
    assert cli.main(["link", "--dir", str(target)]) == 0
    out = capsys.readouterr().out
    assert f"wrote {target / 'locks'}" in out and "is not on PATH" in out
    assert link.MARKER in (target / "locks").read_text()
    monkeypatch.setenv("PATH", f"{target}:/usr/bin")
    assert cli.main(["link", "--dir", str(target)]) == 0
    assert "not on PATH" not in capsys.readouterr().out
    assert cli.main(["link", "--dir", str(target), "--remove"]) == 0
    assert "removed" in capsys.readouterr().out and not (target / "locks").exists()
    assert cli.main(["link", "--dir", str(target), "--remove"]) == 0
    assert "no `locks link` shim" in capsys.readouterr().out
    (target / "locks").write_text("someone else's")
    assert cli.main(["link", "--dir", str(target)]) == 1
    assert "was not written by `locks link`" in capsys.readouterr().err
    monkeypatch.setenv("HOME", str(tmp_path))
    assert link.default_dir(os.environ) == tmp_path / ".local" / "bin"


def _plugin(config: Path, version: str, *, executable: bool = True, manifest: str | None = None) -> Path:
    root = config / "plugins" / "cache" / "rixmerz" / "locks" / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(manifest or f'{{"name": "locks", "version": "{version}"}}')
    launcher = root / "bin" / "locks"
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


def test_a_session_behind_the_newest_install_is_told_to_reload(tmp_path):
    from locks import __version__

    config = tmp_path / "config"
    _plugin(config, __version__)
    assert cli._behind({"CLAUDE_CONFIG_DIR": str(config)}) is None
    _plugin(config, "999.0.0")
    warning = cli._behind({"CLAUDE_CONFIG_DIR": str(config)})
    assert warning is not None
    assert f"runs locks {__version__}, but 999.0.0 is installed" in warning
    assert "/reload-plugins --force" in warning
    assert cli._behind({"CLAUDE_CONFIG_DIR": str(tmp_path / "none")}) is None


def test_a_command_forwarded_from_an_older_plugin_says_the_session_s_skills_are_that_one_s(tmp_path, monkeypatch):
    from locks import __version__

    monkeypatch.setattr(link, "LOADED", "0.31.0")
    warning = cli._behind({"CLAUDE_CONFIG_DIR": str(tmp_path / "none")})
    assert warning.startswith(f"locks: this session loaded locks 0.31.0; the commands it types run {__version__}")
    assert "skills, hooks and /locks:* text are still 0.31.0's" in warning and "/locks:relay" in warning
    assert "/reload-plugins --force" in warning
    monkeypatch.setattr(link, "LOADED", __version__)
    assert cli._behind({"CLAUDE_CONFIG_DIR": str(tmp_path / "none")}) is None


def _copy(config: Path, version: str, script: str | None = None) -> Path:
    root = config / "plugins" / "cache" / "market" / "locks" / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(f'{{"version": "{version}"}}')
    (root / "bin").mkdir()
    launcher = root / "bin" / "locks"
    launcher.write_text(script or "#!/bin/sh\necho old\n")
    launcher.chmod(0o755)
    return root


def test_a_typed_command_runs_the_newest_copy(tmp_path):
    """A session's PATH kept 0.6.13 after 0.6.16 was installed: `locks prune` was
    an invalid choice. A command forwards to the newest; a hook and a checkout never."""
    config = tmp_path / "claude"
    env = {"CLAUDE_CONFIG_DIR": str(config)}
    old = _copy(config, "0.6.13")
    newest = _copy(config, "0.6.16")
    own = str(old / "src" / "locks" / "__main__.py")
    assert link.forward_to(["prune"], env, own, "0.6.13") == newest / "bin" / "locks"
    assert link.forward_to(["hook", "Stop"], env, own, "0.6.13") is None
    assert link.forward_to(["prune"], env, own, "0.6.16") is None
    assert link.forward_to(["prune"], env, "/work/locks/src/locks/__main__.py", "0.6.13") is None
    assert link.forward_to(["prune"], {"CLAUDE_CONFIG_DIR": str(tmp_path / "none")}, own, "0.6.13") is None


def test_an_old_launcher_execs_the_newest_once(tmp_path):
    config = tmp_path / "claude"
    old = _copy(config, "0.0.1")
    (old / "src").symlink_to(Path(__file__).resolve().parents[1] / "src")
    _copy(config, "999.0.0", "#!/bin/sh\necho \"newest ran: $* forwarded=${LOCKS_FORWARDED:-}\"\n")
    (old / "bin" / "locks").write_text((Path(__file__).resolve().parents[1] / "bin" / "locks").read_text())
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config), "LOCKS_PYTHON": sys.executable}
    ran = subprocess.run([str(old / "bin" / "locks"), "matrix"], env=env, capture_output=True, text=True, check=False)
    from locks import __version__

    # the newest copy is told which version forwarded to it: the one the session loaded
    assert ran.stdout.strip() == f"newest ran: matrix forwarded={__version__}"
    hook = subprocess.run([str(old / "bin" / "locks"), "hook", "Nope"], env={**env, "LOCKS_HOOKS_OFF": "1"},
                          input="{}", capture_output=True, text=True, check=False)
    assert "newest ran" not in hook.stdout


def test_a_reload_clears_the_hint_the_launcher_on_the_path_keeps_giving(tmp_path, monkeypatch):
    from locks import hooks

    home = tmp_path / "home"
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "none"), "LOCKS_HOME": str(home), "LOCKS_SESSION_ID": "s1"}
    monkeypatch.setattr(link, "LOADED", "0.31.0")  # the PATH still holds the launcher it started with
    assert "this session loaded locks 0.31.0" in cli._behind(env)  # nothing says it reloaded yet
    link.record_loaded(home, "s1", "0.31.0")  # its hooks still run the old plugin
    assert "this session loaded locks 0.31.0" in cli._behind(env)
    hooks._record_loaded(home, "s1")  # /reload-plugins: its hooks now run this version
    assert cli._behind(env) is None
    assert "this session loaded locks 0.31.0" in cli._behind(env | {"LOCKS_SESSION_ID": "s2"})  # per session


def test_a_hook_run_from_an_older_install_brings_the_hint_back(tmp_path, monkeypatch):
    from locks import __version__

    monkeypatch.setattr(link, "LOADED", "0.31.0")
    link.record_loaded(tmp_path, "s", __version__)
    link.record_loaded(tmp_path, "s", "0.31.0")  # a hook after a compaction ran from an old folder
    assert link.session_loaded(tmp_path, "s") == "0.31.0"
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "none"), "LOCKS_HOME": str(tmp_path), "LOCKS_SESSION_ID": "s"}
    assert "still 0.31.0's" in cli._behind(env)


def test_the_loaded_record_keeps_only_the_newest_sessions_and_survives_junk(tmp_path):
    path = tmp_path / "work" / link.LOADED_BY_HOOKS
    path.parent.mkdir()
    path.write_text("[1]")
    assert link.session_loaded(tmp_path, "s0") is None
    for n in range(link.LOADED_KEPT + 5):
        link.record_loaded(tmp_path, f"s{n}", "1.0.0")
    assert link.session_loaded(tmp_path, "s0") is None
    assert link.session_loaded(tmp_path, f"s{link.LOADED_KEPT + 4}") == "1.0.0"
    assert link.session_loaded(tmp_path, None) is None
