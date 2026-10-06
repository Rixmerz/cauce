from __future__ import annotations

import json

import pytest

from cauce import runtime

from .conftest import git


def _nvm(tmp_path, *versions):
    for v in versions:
        b = tmp_path / "nvm" / "versions" / "node" / f"v{v}" / "bin"
        b.mkdir(parents=True)
        (b / "node").write_text("")
    return {"NVM_DIR": str(tmp_path / "nvm"), "PATH": "/usr/bin"}


@pytest.mark.parametrize(
    ("spec", "fits"),
    [("22", ["22.16.0", "22.23.2"]), ("v22.23.2", ["22.23.2"]), ("22.x", ["22.16.0", "22.23.2"]),
     (">=22.22.3", ["22.23.2", "24.1.0"]), ("^22.22.3", ["22.23.2"]), ("~22.16", ["22.16.0"]),
     (">=22 <24", ["22.16.0", "22.23.2"]), ("20 || >=24", ["20.1.0", "24.1.0"]), ("lts/*", []), ("node", [])],
)
def test_a_declared_version_matches_as_nvm_and_engines_write_it(spec, fits):
    every = [(20, 1, 0), (22, 16, 0), (22, 23, 2), (24, 1, 0)]
    assert [".".join(map(str, v)) for v in every if runtime.satisfies(v, spec)] == fits


def test_the_project_s_node_goes_first_on_the_path(tmp_path):
    env = _nvm(tmp_path, "22.16.0", "22.23.2", "24.1.0")
    project = tmp_path / "app"
    (project / "src").mkdir(parents=True)
    git(project, "init", "-q")
    assert runtime.node_bin(project / "src", env) is None and runtime.env_for(project, env) == {}
    (project / "package.json").write_text(json.dumps({"engines": {"node": ">=22.22.3 <24"}}))
    node = runtime.node_bin(project / "src", env)
    assert node.version == "22.23.2" and node.source == "package.json engines.node"
    assert runtime.env_for(project, env)["PATH"].startswith(str(node.bin))
    (project / ".nvmrc").write_text("v22.16\n")
    assert runtime.node_bin(project, env).version == "22.16.0"
    (project / ".nvmrc").write_text("18\n")
    assert runtime.node_bin(project, env) is None  # declared, not installed: nothing is guessed
    assert runtime.installed({"NVM_DIR": str(tmp_path / "none")}) == []


def test_a_folder_of_projects_gets_the_node_that_fits_them_all(tmp_path):
    env = _nvm(tmp_path, "22.16.0", "22.23.2")
    holder = tmp_path / "work"
    for name, spec in (("web", ">=22.22.3"), ("api", "22")):
        (holder / name).mkdir(parents=True)
        git(holder / name, "init", "-q")
        (holder / name / ".node-version").write_text(spec)
    node = runtime.node_bin(holder, env)
    assert node.version == "22.23.2" and node.source == "the projects in this folder"
