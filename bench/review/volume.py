"""The `volume` variant: the same change, inside a refactor that moves every rule into a folder
per stage and renames the rule methods (`apply_line` to `on_line`, `apply_order` to `on_order`),
over a catalog of 24 generated rules. Five bugs hide in hunks that look mechanical."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

TAGS = {"garden": 12, "kitchen": 8, "toys": 10, "books": 5, "outdoor": 15, "pets": 7, "tools": 9, "office": 6,
        "sports": 11, "music": 13, "bath": 4, "beauty": 14, "baby": 10, "crafts": 8, "auto": 6, "party": 20,
        "travel": 9, "games": 12, "lighting": 7, "storage": 5}
TIERS = {"tier500": (50000, 2), "tier1000": (100000, 3), "tier2000": (200000, 4), "tier5000": (500000, 5)}

TAG_RULE = '''from tally.lines import amount, tags_of
from tally.money import pct
from tally.rules.base import Adjustment, Rule


class {cls}(Rule):
    """Lines tagged {tag} get {p}% off that line."""

    name = "{tag}"
    stage = "line"

    def apply_line(self, line, customer):
        if "{tag}" in tags_of(line):
            return [Adjustment("{tag}", -pct(amount(line), {p}))]
        return []
'''
TIER_RULE = '''from tally.money import pct
from tally.rules.base import Adjustment, Rule


class {cls}(Rule):
    """Orders of {money} or more get {p}% off."""

    name = "{name}"
    stage = "order"
    THRESHOLD = {cents}

    def apply_order(self, subtotal_cents, cart, customer):
        if subtotal_cents >= self.THRESHOLD:
            return [Adjustment("{name}", -pct(subtotal_cents, {p}))]
        return []
'''
TAG_TEST = '''from tally.rules.{mod} import {cls}


def test_{tag}_lines_get_a_discount():
    line = {{"name": "x", "price": 1000, "qty": 1, "tags": "{tag}"}}
    assert {cls}().apply_line(line, {{}})[0].cents < 0
    assert {cls}().apply_line({{**line, "tags": "other"}}, {{}}) == []
'''
TIER_TEST = '''from tally.rules.{mod} import {cls}


def test_{name}_takes_its_percent_over_the_threshold():
    assert {cls}().apply_order({cents} * 2, {{}}, {{}})[0].cents < 0
    assert {cls}().apply_order({cents} // 2, {{}}, {{}}) == []
'''


def _cls(name: str) -> str:
    return name.capitalize()


def catalog(where: Path, moved: bool) -> None:
    """The 24 rules, their tests and the docs' table, where the base or the change keeps them."""
    for tag, p in TAGS.items():
        mod = f"line.{tag}" if moved else tag
        (where / "src/tally/rules" / f"{mod.replace('.', '/')}.py").write_text(TAG_RULE.format(cls=_cls(tag), tag=tag,
                                                                                                p=p))
        (where / "tests" / f"test_rule_{tag}.py").write_text(TAG_TEST.format(mod=mod, cls=_cls(tag), tag=tag))
    for name, (cents, p) in TIERS.items():
        mod = f"order.{name}" if moved else name
        (where / "src/tally/rules" / f"{mod.replace('.', '/')}.py").write_text(TIER_RULE.format(
            cls=_cls(name), name=name, cents=cents, p=p, money=f"{cents // 100}.00"))
        (where / "tests" / f"test_rule_{name}.py").write_text(TIER_TEST.format(mod=mod, cls=_cls(name), name=name,
                                                                              cents=cents))


def registry(where: Path, moved: bool, extra: tuple[str, ...] = ()) -> None:
    names = [*(("line", t) for t in TAGS), *(("order", t) for t in TIERS)]
    imports = [f"from tally.rules.{f'{stage}.' if moved else ''}{n} import {_cls(n)}" for stage, n in names]
    imports += [f"from tally.rules.{m} import {c}" for m, c in
                (("clearance", "Clearance"), ("staff", "Staff"), *(e.split(":") for e in extra))]
    classes = ["Clearance", "Staff", *(e.split(":")[1] for e in extra), *(_cls(n) for _, n in names)]
    body = "\n".join(sorted(imports)) + "\n\nRULES = {rule.name: rule for rule in (\n" + "".join(
        f"    {c}(),\n" for c in classes) + ")}\n"
    (where / "src/tally/rules/__init__.py").write_text(body)


def docs_table(where: Path, extra_rows: str = "") -> None:
    rows = [f"| {t} | line | Lines tagged {t} get {p}% off |" for t, p in TAGS.items()]
    rows += [f"| {n} | order | Orders of {c // 100}.00 or more get {p}% off |" for n, (c, p) in TIERS.items()]
    path = where / "docs/rules.md"
    head = path.read_text() if path.exists() else "# Pricing rules\n\n| Rule | Stage | What it does |\n|---|---|---|\n"
    path.write_text(head.rstrip("\n") + "\n" + extra_rows + "\n".join(rows) + "\n")


def base(where: Path) -> None:
    (where / "docs").mkdir(exist_ok=True)
    catalog(where, moved=False)
    registry(where, moved=False)
    docs_table(where)
    cfg = where / "src/tally/rules.cfg"
    cfg.write_text(cfg.read_text() + "".join(f"{n}\n" for n in [*TAGS, *TIERS]))


def change(where: Path, overlay: Path) -> None:
    """The change of the plain variant, then the refactor over the whole catalog, with five slips."""
    run = lambda *a: subprocess.run(a, cwd=where, check=True, capture_output=True)  # noqa: E731
    for stage in ("line", "order"):
        (where / "src/tally/rules" / stage).mkdir(exist_ok=True)
        (where / "src/tally/rules" / stage / "__init__.py").write_text("")
    for tag in TAGS:
        run("git", "mv", f"src/tally/rules/{tag}.py", f"src/tally/rules/line/{tag}.py")
    for name in TIERS:
        run("git", "mv", f"src/tally/rules/{name}.py", f"src/tally/rules/order/{name}.py")
    for tag in TAGS:  # the tests' imports follow the move
        t = where / "tests" / f"test_rule_{tag}.py"
        t.write_text(t.read_text().replace(f"tally.rules.{tag} ", f"tally.rules.line.{tag} "))
    for name in TIERS:
        t = where / "tests" / f"test_rule_{name}.py"
        t.write_text(t.read_text().replace(f"tally.rules.{name} ", f"tally.rules.order.{name} "))
    import shutil
    shutil.copytree(overlay, where, dirs_exist_ok=True)
    registry(where, moved=True, extra=("promo:Promo", "bulk:Bulk", "loyalty:Loyalty"))
    (where / "docs/rules.md").write_text((overlay / "docs/rules.md").read_text())
    docs_table(where)
    cfg = where / "src/tally/rules.cfg"
    cfg.write_text(cfg.read_text() + "".join(f"{n}\n" for n in [*TAGS, *TIERS]))
    keep = {where / "src/tally/rules/line/books.py", where / "tests/test_rule_books.py"}
    for path in [*where.glob("src/**/*.py"), *where.glob("tests/*.py")]:
        if path in keep:
            continue
        text = path.read_text()
        new = re.sub(r"\bapply_line\b", "on_line", re.sub(r"\bapply_order\b", "on_order", text))
        if new != text:
            path.write_text(new)
    slip = {"line/garden.py": ("pct(amount(line), 12)", "pct(amount(line), 21)"),
            "order/tier1000.py": ("subtotal_cents >= self.THRESHOLD", "subtotal_cents > self.THRESHOLD"),
            "line/pets.py": ('"pets" in tags_of(line)', '"pets" in (line.get("tags") or "").lower()'),
            "line/toys.py": ('stage = "line"', 'stage = "order"')}
    for rel, (old, new) in slip.items():
        path = where / "src/tally/rules" / rel
        path.write_text(path.read_text().replace(old, new))
