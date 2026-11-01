"""`scripts/rename_to_locks.py`: the rename as a script, run on a small tree of the old name."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from .conftest import git

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rename_to_locks.py"
spec = importlib.util.spec_from_file_location("rename_to_locks", SCRIPT)
rename = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rename)


def _tree(root: Path) -> Path:
    files = {
        "src/cauce/__init__.py": '"""cauce: one core."""\n',
        "src/cauce/store.py": "from cauce import text\nimport cauce.text\nHOME = 'CAUCE_HOME'\n"
                              "BRANCH = 'cauce/task-1'\nFENCE = 'cauce-result'\n",
        "bin/cauce": "#!/bin/sh\nexec python3 -m cauce \"$@\"\n",
        "hooks/cauce.tsx": "const T = 'mcp__cauce__queue' // /cauce:init\nconst S = 'cauce\\'s'\n",
        "README.md": "# cauce\n\nRun `cauce run`. Read from cauce's log; .cauce/ holds it. "
                     "See https://github.com/Rixmerz/cauce.\nCauce-Task: #1 and X-Cauce-Token\n",
        "keep.py": "OLD = 'cauce'  # legacy-name: on purpose\n",
        "CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n\n- cauce now does X.\n\n## [0.1.0] - 2026-01-01\n\n"
                        "- cauce did Y.\n",
        "uv.lock": 'version = 1\n\n[[package]]\nname = "cauce"\nsource = { editable = "." }\nnote = "cauce"\n',
        "pyproject.toml": '[project]\nname = "cauce"\n\n[project.scripts]\ncauce = "cauce.cli:main"\n',
        "image.png": b"\x89PNG\r\n\x1a\n\xff\xfecauce",
    }
    root.mkdir()
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text)
    (root / "bin" / "cauce").chmod(0o755)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    return root


def test_the_rename_moves_rewrites_keeps_what_it_must_and_is_idempotent(tmp_path, capsys):
    root = _tree(tmp_path / "t")
    assert rename.main(["--check", "--root", str(root)]) == 1
    assert "un-renamed" in capsys.readouterr().out
    assert rename.main(["--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "src/cauce/ -> src/locks/" in out and "bin/cauce -> bin/locks" in out and "hooks/cauce.tsx" in out
    assert "import (from/import cauce)" in out and "environment (CAUCE_)" in out
    assert (root / "src/locks/store.py").read_text() == ("from locks import text\nimport locks.text\n"
                                                        "HOME = 'LOCKS_HOME'\nBRANCH = 'locks/task-1'\n"
                                                        "FENCE = 'locks-result'\n")
    readme = (root / "README.md").read_text()
    assert readme == ("# locks\n\nRun `locks run`. Read from locks' log; .locks/ holds it. "
                      "See https://github.com/Rixmerz/locks.\nLocks-Task: #1 and X-Locks-Token\n")
    assert (root / "hooks/locks.tsx").read_text() == ("const T = 'mcp__locks__queue' // /locks:init\n"
                                                     "const S = 'locks\\''\n")
    assert (root / "keep.py").read_text() == "OLD = 'cauce'  # legacy-name: on purpose\n"
    changelog = (root / "CHANGELOG.md").read_text()
    assert "- locks now does X." in changelog and "- cauce did Y." in changelog and rename.RENAME_NOTE in changelog
    lock = (root / "uv.lock").read_text()
    assert 'name = "locks"' in lock and 'note = "cauce"' in lock
    pyproject = (root / "pyproject.toml").read_text()
    assert 'name = "locks"' in pyproject and 'locks = "locks.cli:main"' in pyproject
    assert rename.CONSOLE_ALIAS in pyproject
    assert (root / "bin/locks").read_text() == '#!/bin/sh\nexec python3 -m locks "$@"\n'
    assert (root / "bin/cauce").read_text() == rename.SHIM and os.access(root / "bin/cauce", os.X_OK)
    assert (root / "image.png").read_bytes().endswith(b"cauce")
    assert rename.main(["--check", "--root", str(root)]) == 0
    capsys.readouterr()
    before = git(root, "status", "--porcelain")
    assert rename.main(["--root", str(root)]) == 0
    assert "moved 0 path(s)" in capsys.readouterr().out and git(root, "status", "--porcelain") == before


def test_this_repository_has_no_old_name_left_outside_the_allow_list():
    proc = subprocess.run([sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout
