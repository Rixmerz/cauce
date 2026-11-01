"""Teams: the roles a project's workers may be, chosen by a person.

A machine runs several sessions on several projects at once, each with its own
purpose, so a person keeps several teams and gives each project one:

- `locks`, the team locks ships: every bundled role (a role of the person's
  that shadows a bundled one stands in for it). A project with no team of its
  own uses it;
- the person's own, `<locks home>/teams/<name>.json`:
  `{"name": ..., "description": ..., "roles": [role, ...]}`, the roles in the
  order that breaks a tie between two that take the same kind.

Which team a project uses is kept in the person's locks home, keyed by the
repository (`locks team use`), never in the repository: a team decides what
a worker is told, and a repository must not be able to choose that by
committing a file.

A project's team is the whole of what its workers may be. A task is classified
only into the kinds its team covers, so a team without a role for reviews
never runs a review as the bundled reviewer.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from locks import config, legacy, roles
from locks.store import home

BUNDLED = "locks"


class TeamError(ValueError):
    """A team that cannot be used or changed as asked; the message says why."""


@dataclass(frozen=True)
class Team:
    name: str
    description: str
    role_names: tuple[str, ...]
    scope: str = "user"
    path: str = ""

    def members(self, catalog: roles.Catalog) -> tuple[list[roles.Role], list[str]]:
        """The roles this team names that exist, in its order, and the names that do not."""
        found, missing = [], []
        for name in self.role_names:
            if name in catalog.roles:
                found.append(catalog.roles[name])
            else:
                missing.append(name)
        return found, missing

    def kinds(self, catalog: roles.Catalog) -> tuple[str, ...]:
        return roles.kinds_of(self.members(catalog)[0])


@dataclass
class Staffing:
    """What one project's team gives a task of one kind."""
    team: Team
    role: roles.Role | None
    why: str
    covered: tuple[str, ...] = ()
    missing: list[str] = field(default_factory=list)


def folder() -> Path:
    return home() / "teams"


def bundled(catalog: roles.Catalog) -> Team:
    return Team(BUNDLED, "the team locks ships: one role for every kind of work",
                tuple(r.name for r in catalog.bundled()), scope="bundled")


def _read(path: Path) -> Team:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TeamError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TeamError(f"{path} must hold an object")
    names = data.get("roles")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise TeamError(f"{path}: `roles` is a list of role names")
    description = data.get("description") if isinstance(data.get("description"), str) else ""
    return Team(path.stem, description, tuple(dict.fromkeys(names)), path=str(path))


def listing(catalog: roles.Catalog | None = None) -> tuple[list[Team], dict[str, str]]:
    """The bundled team and every team of the person's, and the files refused."""
    catalog = catalog or roles.load()
    teams, refused = [bundled(catalog)], {}
    if folder().is_dir():
        for path in sorted(folder().glob("*.json")):
            if not roles.NAME.match(path.stem) or path.stem == BUNDLED:
                refused[path.stem] = f"{path}: not a team name"
                continue
            try:
                teams.append(_read(path))
            except TeamError as exc:
                refused[path.stem] = str(exc)
    return teams, refused


def get(name: str, catalog: roles.Catalog | None = None) -> Team:
    catalog = catalog or roles.load()
    if name in (BUNDLED, legacy.TEAM):  # the bundled team, by its old name too
        return bundled(catalog)
    path = folder() / f"{name}.json"
    if not roles.NAME.match(name) or not path.is_file():
        known = ", ".join(t.name for t in listing(catalog)[0])
        raise TeamError(f"no team {name!r}; known: {known}")
    return _read(path)


def _write(team: Team, catalog: roles.Catalog) -> Path:
    unknown = [n for n in team.role_names if n not in catalog.roles]
    if unknown:
        raise TeamError(f"no role {', '.join(map(repr, unknown))}; `locks roles list` shows them")
    if not team.role_names:
        raise TeamError("a team needs at least one role")
    path = folder() / f"{team.name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"name": team.name, "description": team.description,
                               "roles": list(team.role_names)}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def create(name: str, role_names: Sequence[str], *, description: str = "", start_from: str | None = None,
           catalog: roles.Catalog | None = None) -> Path:
    """A new team of the person's: the roles named, after the roles of `start_from`."""
    catalog = catalog or roles.load()
    if not roles.NAME.match(name):
        raise TeamError(f"{name!r} is no team name: lowercase letters, digits, - and _")
    if name in (BUNDLED, legacy.TEAM):
        raise TeamError(f"{name!r} is the team locks ships; pick another name")
    if (folder() / f"{name}.json").exists():
        raise TeamError(f"there is already a team {name!r}")
    base = get(start_from, catalog).role_names if start_from else ()
    return _write(Team(name, description, tuple(dict.fromkeys([*base, *role_names]))), catalog)


def change(name: str, *, add: Sequence[str] = (), drop: Sequence[str] = (), description: str | None = None,
           catalog: roles.Catalog | None = None) -> Path:
    catalog = catalog or roles.load()
    if name == BUNDLED:
        raise TeamError(f"the {BUNDLED!r} team is not changed: make your own with "
                        f"`locks team create <name> --from {BUNDLED}`")
    team = get(name, catalog)
    gone = set(drop)
    names = tuple(dict.fromkeys([*(n for n in team.role_names if n not in gone), *add]))
    return _write(Team(name, team.description if description is None else description, names), catalog)


def delete(name: str) -> Path:
    if name == BUNDLED:
        raise TeamError(f"the {BUNDLED!r} team is not deleted")
    path = folder() / f"{name}.json"
    if not path.is_file():
        raise TeamError(f"no team {name!r}")
    users = [repo for repo, team in assignments().items() if team == name]
    if users:
        raise TeamError(f"{name!r} is the team of {', '.join(users)}; give them another first "
                        "(`locks team use <team>` there)")
    path.unlink()
    return path


# --- which team a project uses -----------------------------------------------------


def assignments(env: Mapping[str, str] | None = None) -> dict[str, str]:
    stored = config.load(env).get("teams") or {}
    return {str(k): str(v) for k, v in stored.items() if isinstance(v, str)} if isinstance(stored, dict) else {}


def use(repo: str, name: str | None, env: Mapping[str, str] | None = None) -> None:
    """Give a project a team; None gives it back the bundled one."""
    every = assignments(env)
    if name is None or name in (BUNDLED, legacy.TEAM):
        every.pop(repo, None)
    else:
        get(name)
        every[repo] = name
    config.save({"teams": every}, env)


def of(repo: str | None, catalog: roles.Catalog | None = None) -> tuple[Team, str]:
    """A project's team, and a word on why. A team that is gone falls back to the
    bundled one, and says so."""
    catalog = catalog or roles.load()
    name = assignments().get(repo or "")
    if not name:
        return bundled(catalog), "no team chosen here: the bundled one"
    try:
        return get(name, catalog), f"the team chosen for this project ({name})"
    except TeamError:
        return bundled(catalog), f"the team chosen here ({name}) is gone: the bundled one"


def projects_of(name: str) -> list[str]:
    return sorted(repo for repo, team in assignments().items() if team == name)


def staff(kind: str, repo: str | None, repo_dir: Path, catalog: roles.Catalog | None = None) -> Staffing:
    """The role a task of `kind` runs as in this project."""
    catalog = catalog or roles.load()
    team, _ = of(repo, catalog)
    members, missing = team.members(catalog)
    role, why = roles.pick(kind, members, repo_dir)
    return Staffing(team, role, why, roles.kinds_of(members), missing)


def describe(team: Team, catalog: roles.Catalog) -> dict[str, Any]:
    """A team with every member in full, for the UI and `locks team show`."""
    members, missing = team.members(catalog)
    return {"name": team.name, "description": team.description, "scope": team.scope, "path": team.path,
            "kinds": list(roles.kinds_of(members)), "missing": missing, "projects": projects_of(team.name)
            if team.name != BUNDLED else [],
            "roles": [{**r.summary(), "prompt": r.prompt} for r in members]}
