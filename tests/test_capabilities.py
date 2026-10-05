from __future__ import annotations

import json
from pathlib import Path

import pytest

from cauce import capabilities as caps


def write(tmp_path: Path, data) -> Path:
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps(data))
    return path


def test_selection_by_kind_and_after_failure(tmp_path):
    registry = caps.load(write(tmp_path, caps.EXAMPLE))
    ui = caps.select(registry, "ui", workdir=Path("/w"))
    assert ui.names == ("livespec", "layout-inspector")
    first = caps.select(registry, "implement", workdir=Path("/w"))
    assert first.names == ("livespec",)
    retry = caps.select(registry, "implement", failed_before=True, workdir=Path("/w"))
    assert retry.names == ("livespec", "layout-inspector")
    assert 'workspace="/w"' in first.system_prompt()
    assert caps.Selection().system_prompt() == ""


def test_workdir_reaches_args_and_env(tmp_path):
    registry = caps.load(write(tmp_path, {"idx": {"server": {"command": "x", "args": ["--root", "{workdir}"],
                                                             "env": {"ROOT": "{workdir}"}, "port": 3}}}))
    server = caps.select(registry, "docs", workdir=Path("/repo")).servers["idx"]
    assert server == {"command": "x", "args": ["--root", "/repo"], "env": {"ROOT": "/repo"}, "port": 3}


def test_absent_registry_is_empty_and_bad_ones_are_named(tmp_path):
    assert caps.load(tmp_path / "missing.json") == {}
    for bad in ([], {"x": {}}, {"x": {"server": {}, "when": "sometimes"}}):
        with pytest.raises(caps.RegistryError):
            caps.load(write(tmp_path, bad))
    (tmp_path / "capabilities.json").write_text("{nope")
    with pytest.raises(caps.RegistryError):
        caps.load(tmp_path / "capabilities.json")
