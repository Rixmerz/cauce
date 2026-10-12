"""`cauce roles …` and `cauce team …`: the roles workers may be, and the teams a
project's workers come from.

Kept apart from `cli.py`; `cli.build_parser` adds both. Every command that
writes says where it wrote. Nothing here grants anything: a role names the
person's capabilities, and a team chooses among roles.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cauce import repo, roles, teams


def _key(args: argparse.Namespace) -> str | None:
    return repo.key(Path(getattr(args, "repo", None) or os.getcwd()).resolve())


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


# --- roles -----------------------------------------------------------------------------


def cmd_roles(args: argparse.Namespace) -> int:
    try:
        return _ROLES[args.roles_command or "list"](args)
    except (roles.RoleError, teams.TeamError) as exc:
        return _fail(str(exc))


def _roles_list(args: argparse.Namespace) -> int:
    catalog = roles.load()
    listed = [r for r in catalog.roles.values() if not args.kind or args.kind in r.kinds]
    if args.json:
        print(json.dumps({"roles": [r.summary() for r in listed], "refused": catalog.refused}, indent=2))
        return 0
    for r in listed:
        mark = " (shadows the bundled one)" if r.shadows else ""
        print(f"{r.name:<18} {r.scope:<8} {', '.join(r.kinds):<28} {r.description[:70]}{mark}")
    for name, why in sorted(catalog.refused.items()):
        print(f"{name:<18} refused  {why}")
    return 0


def _roles_show(args: argparse.Namespace) -> int:
    catalog = roles.load()
    role = catalog.get(args.name)
    briefing = roles.brief(role)
    if args.json:
        print(json.dumps({**role.summary(), "prompt": role.prompt, "carried": list(briefing.skills),
                          "missing": list(briefing.missing), "system_prompt": briefing.system_prompt}, indent=2))
        return 0
    print(f"{role.name} ({role.scope}{', shadows the bundled one' if role.shadows else ''}): {role.description}")
    print(f"  file          {role.path}")
    print(f"  kinds         {', '.join(role.kinds)}")
    for key in ("extends", "derived_from"):
        if getattr(role, key):
            print(f"  {key:<13} {getattr(role, key)}")
    for key in ("skills", "on_demand", "capabilities", "disallowed_tools", "match"):
        values = getattr(role, key)
        if values:
            print(f"  {key:<13} {', '.join(values)}")
    if briefing.missing:
        print(f"  missing       {', '.join(briefing.missing)} (not found in any skill folder)")
    print(f"  in teams      {', '.join(t.name for t in teams.listing(catalog)[0] if role.name in t.role_names)}")
    print()
    print(briefing.system_prompt if args.full else role.prompt)
    return 0


def _roles_copy(args: argparse.Namespace) -> int:
    path = roles.copy(args.source, args.name, kinds=args.kind, description=args.description)
    print(f"wrote {path}: edit it; it is used where a team names {args.name!r}"
          + (" (it shadows the bundled role everywhere)" if args.name == args.source else ""))
    return 0


def _roles_rm(args: argparse.Namespace) -> int:
    path = roles.remove(args.name)
    print(f"removed {path}")
    return 0


def _roles_check(args: argparse.Namespace) -> int:
    catalog = roles.load()
    problems = [f"{name}: {why}" for name, why in sorted(catalog.refused.items())]
    for role in catalog.roles.values():
        missing = roles.brief(role).missing
        if missing:
            problems.append(f"{role.name}: skill(s) not found: {', '.join(missing)}")
    team_list, refused = teams.listing(catalog)
    problems += [f"team {name}: {why}" for name, why in sorted(refused.items())]
    for team in team_list:
        missing = team.members(catalog)[1]
        if missing:
            problems.append(f"team {team.name}: no role {', '.join(missing)}")
    print("\n".join(problems) if problems else f"{len(catalog.roles)} roles and {len(team_list)} teams check out")
    return 1 if problems else 0


def _skills(args: argparse.Namespace) -> int:
    for skill in roles.bundled_skills():
        print(f"{skill.name:<22} {skill.description[:90]}")
    return 0


_ROLES = {"list": _roles_list, "show": _roles_show, "copy": _roles_copy, "rm": _roles_rm, "check": _roles_check,
          "skills": _skills}


# --- teams -----------------------------------------------------------------------------


def cmd_team(args: argparse.Namespace) -> int:
    try:
        return _TEAM[args.team_command or "show"](args)
    except (roles.RoleError, teams.TeamError) as exc:
        return _fail(str(exc))


def _team_list(args: argparse.Namespace) -> int:
    catalog = roles.load()
    found, refused = teams.listing(catalog)
    here = teams.of(_key(args), catalog)[0].name
    if args.json:
        print(json.dumps({"teams": [teams.describe(t, catalog) for t in found], "refused": refused,
                          "here": here}, indent=2))
        return 0
    for team in found:
        mark = "*" if team.name == here else " "
        users = teams.projects_of(team.name)
        print(f"{mark} {team.name:<18} {len(team.role_names):>2} roles  {team.description[:60]}"
              + (f"  [{len(users)} project(s)]" if users else ""))
    for name, why in sorted(refused.items()):
        print(f"  {name:<18} refused: {why}")
    return 0


def _team_show(args: argparse.Namespace) -> int:
    catalog = roles.load()
    if args.name:
        team, why = teams.get(args.name, catalog), ""
    else:
        team, why = teams.of(_key(args), catalog)
    data = teams.describe(team, catalog)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"{team.name}: {team.description}" + (f"  ({why})" if why else ""))
    print(f"  covers   {', '.join(data['kinds'])}")
    if data["projects"]:
        print(f"  used by  {', '.join(data['projects'])}")
    for r in data["roles"]:
        print(f"  - {r['name']:<18} {', '.join(r['kinds']):<28} {r['description'][:60]}")
    for name in data["missing"]:
        print(f"  - {name:<18} (no such role)")
    return 0


def _team_create(args: argparse.Namespace) -> int:
    path = teams.create(args.name, args.role or (), description=args.description or "", start_from=args.start_from)
    print(f"wrote {path}; give it to a project with `cauce team use {args.name}` there")
    return 0


def _team_change(args: argparse.Namespace) -> int:
    path = teams.change(args.name, add=args.add or (), drop=args.drop or (), description=args.description)
    print(f"wrote {path}")
    return 0


def _team_delete(args: argparse.Namespace) -> int:
    print(f"removed {teams.delete(args.name)}")
    return 0


def _team_use(args: argparse.Namespace) -> int:
    key = _key(args)
    if not key:
        return _fail("this is no directory cauce can key a team to")
    teams.use(key, None if args.none else args.name)
    team = teams.of(key)[0]
    print(f"{key} uses the {team.name} team: {', '.join(team.kinds(roles.load()))}")
    return 0


_TEAM = {"list": _team_list, "show": _team_show, "create": _team_create, "change": _team_change,
         "delete": _team_delete, "use": _team_use}


def add_parser(sub: argparse._SubParsersAction) -> None:
    top = sub.add_parser("roles", help="the roles workers run as: list, show, copy to edit, check")
    top.set_defaults(func=cmd_roles, roles_command="list", kind=None, json=False)
    rsub = top.add_subparsers(dest="roles_command")
    p = rsub.add_parser("list", help="every role, the person's first, and the ones refused")
    p.add_argument("--kind", help="only the roles that take this kind")
    p.add_argument("--json", action="store_true")
    p = rsub.add_parser("show", help="one role: its fields, its skills, its instructions")
    p.add_argument("name")
    p.add_argument("--full", action="store_true", help="the whole system prompt a worker gets, skills included")
    p.add_argument("--json", action="store_true")
    p = rsub.add_parser("copy", help="copy a role into your cauce home to edit: cauce roles copy reviewer my-reviewer")
    p.add_argument("source")
    p.add_argument("name", help="the new role's name; the same name shadows the bundled one")
    p.add_argument("--kind", action="append", help="the kinds it takes (default: the source's)")
    p.add_argument("--description")
    p = rsub.add_parser("rm", help="remove a role of yours (never a bundled one)")
    p.add_argument("name")
    rsub.add_parser("check", help="every role and team, and what is wrong with them")
    rsub.add_parser("skills", help="the skills cauce ships")

    top = sub.add_parser("team", help="teams of roles; a project's workers come only from its team")
    top.set_defaults(func=cmd_team, team_command="show", name=None, json=False, repo=None)
    top.add_argument("--repo", help="the project (default: here)")
    tsub = top.add_subparsers(dest="team_command")
    p = tsub.add_parser("list", help="every team; * marks this project's")
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p = tsub.add_parser("show", help="a team in full (default: this project's)")
    p.add_argument("name", nargs="?")
    p.add_argument("--repo")
    p.add_argument("--json", action="store_true")
    p = tsub.add_parser("create", help="a team of yours: cauce team create web --from cauce --role my-reviewer")
    p.add_argument("name")
    p.add_argument("--role", action="append", help="a role it has (repeatable), after those of --from")
    p.add_argument("--from", dest="start_from", help="start from this team's roles")
    p.add_argument("--description", help="what the team is for")
    p = tsub.add_parser("change", help="add or drop roles: cauce team change web --add tester --drop reviewer")
    p.add_argument("name")
    p.add_argument("--add", action="append")
    p.add_argument("--drop", action="append")
    p.add_argument("--description")
    p = tsub.add_parser("delete", help="delete a team of yours that no project uses")
    p.add_argument("name")
    p = tsub.add_parser("use", help="the team this project's workers come from: cauce team use web; --none "
                                    "for the bundled one")
    p.add_argument("name", nargs="?")
    p.add_argument("--none", action="store_true")
    p.add_argument("--repo")
