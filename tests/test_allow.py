from __future__ import annotations

import pytest

from cauce import allow


@pytest.mark.parametrize(
    ("command", "prefixes"),
    [
        ('~/bin/search -n -i "a|b;c" src; echo "exit $?"; git status --short', ["~/bin/search", "git status"]),
        ("source ~/.nvm/nvm.sh >/dev/null && nvm use 22 >/dev/null && npx tsc --noEmit 2>&1 | tail -15",
         ["source ~/.nvm/nvm.sh", "nvm", "npx tsc", "tail"]),
        ("cd /repo; python3 - <<'EOF'\nimport json; print(1)\nEOF", ["python3"]),
        ("bash -c 'source ~/.nvm/nvm.sh && npm run build'", ["source ~/.nvm/nvm.sh", "npm run build"]),
        ("FOO=1 BAR=2 npm run test -- --watch", ["npm run test"]),
        ("python -m pytest -q tests", ["python -m pytest"]),
        ("npm --prefix web run lint", ["npm"]),
        ('sed -i "s/a/b/" src/x.ts', ["sed"]),
        ("echo hi; cd x; true", []),
        ("$(which node) app.js", []),
        ('npm "unterminated', ["npm"]),
    ],
)
def test_a_shell_line_becomes_one_prefix_per_command_it_chains(command, prefixes):
    assert allow.commands(command) == prefixes


def test_every_tool_gets_a_rule_a_person_can_pass():
    assert allow.rules("Bash", {"command": "npm run build && npx eslint src"}) == [
        "Bash(npm run build:*)", "Bash(npx eslint:*)"]
    assert allow.rules("Bash", {"command": "cd src"}) == ["Bash"]
    # an absolute path takes two slashes; one would be read from the settings file
    assert allow.rules("Write", {"file_path": "/tmp/report.md"}) == ["Write(//tmp/report.md)"]
    assert allow.rules("Read", {"file_path": "//tmp/x"}) == ["Read(//tmp/x)"]
    assert allow.rules("Edit", {"file_path": "src/app.ts"}) == ["Edit(src/app.ts)"]
    assert allow.rules("NotebookEdit", {"notebook_path": "/n.ipynb"}) == ["NotebookEdit(//n.ipynb)"]
    assert allow.rules("WebFetch", {"url": "https://docs.example.org/a"}) == ["WebFetch(domain:docs.example.org)"]
    assert allow.rules("WebFetch", {"url": "not a url"}) == ["WebFetch"]
    assert allow.rules("Glob", None) == ["Glob"]


def test_refusals_recorded_as_calls_are_read_back_to_rules():
    cut = 'Bash(~/bin/search -n "a|b" src; echo "exit $?"; g...)'
    assert allow.from_refusals([cut, "Bash(npx tsc --noEmit)", "Read(/abs/file)", "Glob", "Bash(npx tsc -p x)"]) == (
        "Bash(~/bin/search:*)", "Bash(npx tsc:*)", "Read(//abs/file)", "Glob")
    assert allow.from_refusals(["WebFetch(https://example.org/x)"]) == ("WebFetch(domain:example.org)",)
