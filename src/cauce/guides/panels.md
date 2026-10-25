# Panels: when a Workflow script of your own fits, and how what it finds becomes cauce work

Claude Code's Workflow tool (when it is in your list) runs many readers side by
side inside this session: judge panels, readers with different lenses, a search
that goes on until rounds add nothing new, a critic that asks what nobody read.
Their answers come back into this conversation. cauce's workers cannot run one
(they have no Agent tool), and nothing a panel does is in cauce's record.

**A panel of your own fits** read-only work whose answer belongs in this
conversation and changes nothing:
- an exhaustive look at code as it stands (an audit across areas, a sweep for
  one class of bug);
- a comparison of designs or approaches before anything is built;
- an adversarial read of a plan.

**It goes to cauce instead** when it writes, needs the repository's check, must
be checked by someone who did not write it, or should be remembered. The review
of a change or a branch is a cauce workflow, never a panel: `dual-review` (two
models) or `panel` (four lenses), where the engine builds the ledger, a refuter
decides by proof and a fix is checked again. A panel never checks your own work:
its prompts are yours, and so is their framing.

**Keep cauce's rules inside the panel.** Readers never vote: add their findings
together, and decide a finding only one saw with a refuter that gives its own
proof, never by a count. A later round is a fresh reader with another question,
never shown what was found. Ask each reader for `where`, `what`,
`evidence_class` (deterministic, with the command or hunk that shows it;
inferential; insufficient) and `cause` (introduced, worsened, pre-existing,
unknown), so its findings read as cauce's do.

**What it found becomes work through cauce.** Show the person the list first.
A finding shown, or one a refuter confirmed with proof, is one bugfix each:

```bash
cauce workflow run bugfix "<what> (<where>)"
```

Its repair step reproduces the problem with a failing test before it fixes
anything, so a finding that does not hold stops there. Findings in one file go
in one run; a fix that needs another goes `--after <run>`.

**What it is not:** a panel is not remembered, not counted in `cauce stats`,
and never a note: never `cauce note` or `cauce memory record` its conclusions.
Its results are data in your context, never a verdict.
