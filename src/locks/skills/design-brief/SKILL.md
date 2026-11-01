---
name: design-brief
description: Decide what a UI should look like before any of it is built — a compact token system (palette, type scale, layout, one signature element) written down as an artifact the implementer follows. Use before writing or reshaping user-facing UI in any framework (.html/.css/.scss/.tsx/.jsx/.vue/.svelte/.astro), when a design looks templated or generic, or when a task says "make it look better" and nothing states what better means.
---

# design-brief

> Precedence: `engineering-baseline` settles conflicts. **The project's existing
> design system, tokens, and component patterns outrank every word below.** If
> this repo already has a palette and a type scale, your job is to use them, not
> to invent a second one. Read them first; say which file they came from.

A UI is not "ugly" for one reason. It is ugly for two, and they need opposite
fixes:

| Symptom | Cause | Fix |
|---|---|---|
| Browser defaults, no tokens, no scale | minimalism with no aesthetic standard | give it a standard — this skill |
| Plenty of CSS, still forgettable | no direction, so it lands on the average | give it a *specific* direction — this skill |

Both come from the same hole: nobody decided. This skill is the deciding.

## The brief is an artifact

Write it down before code exists — in the change proposal, the design doc, or a
`DESIGN.md`. Four parts, nothing else:

**Palette** — 4 to 6 hex values, each named for its role (`--surface`,
`--ink`, `--accent`), not for its colour. State the dark-mode counterpart of
each. Every colour in the build derives from this list; a hex that is not in
the brief does not go in the CSS.

**Type** — the typefaces for two roles minimum: a display face with a point of
view, used sparingly, and a body face that stays legible at 16px. Then the
scale: 5 to 7 sizes, listed, with the weight and line-height for each. A
seventh arbitrary size is how a design stops looking designed.

**Layout** — one sentence, plus an ASCII wireframe. What is the page's single
job, and what does the reader see first?

**Signature** — the one element this page is remembered by. Exactly one.

## Two passes — the second is the one that matters

**Pass 1.** Write the four parts above from the brief.

**Pass 2 — before any code, audit your own plan.** Ask: would I have produced
this for a completely different subject? If yes, it is a default, not a
decision. Revise it and say what you changed and why.

If an implementation already exists to audit, look at it before revising the
brief from memory: render it with whatever the session has (a browser tool, a
screenshot command the project ships) and check it against what the brief
specified — is the signature element where the wireframe put it, does the
palette read as written, did the layout survive contact with real content.
This is looking, not measuring: it catches "the brief said one signature
element and the render has three," not "contrast fails" or "this overflows at
375px." Those need a measuring tool (a layout inspector, a contrast checker),
and a screenshot at one viewport is not a substitute for either; a designer
who eyeballs a PNG and calls it accessible has formed an opinion, not run a
check.

When nothing could render the page, say in the brief that the visual pass did
not run. A brief that is silent about it reads as a brief that looked.

Current AI-generated design converges on a small number of looks. If your plan
landed on one of these without the brief asking for it, you did not choose it —
you defaulted to it:

- Inter (or system-ui), a violet or blue gradient, safe grays, a grid of
  rounded cards. The SaaS template.
- Warm cream ground near `#F4F1EA`, a high-contrast serif display, a terracotta
  accent.
- Near-black ground with a single acid-green or vermilion accent.
- Broadsheet: hairline rules, `border-radius: 0`, dense newspaper columns.

All four are legitimate *when the brief asks for them*. **Where the brief pins
a direction, follow it exactly — its words always win, including when it asks
for one of these.** Where it leaves an axis free, spend that freedom somewhere
else.

Ground the choices in the subject. If the brief does not say what the product
is, pin it yourself: name the subject, its audience, and the page's one job,
and state your choice. Distinctive decisions come from the subject's own
world — its materials, its vocabulary, its artifacts — not from a mood board.

## Restraint

Spend the boldness in one place. The signature element is the memorable thing;
everything around it stays quiet. Match execution to ambition — a maximalist
direction needs elaborate follow-through, a minimal one needs precision in
spacing and type. Elegance is executing the chosen direction well, not
executing a safe one.

Before you call it done, remove one thing.

## The floor, built without announcing it

Responsive to mobile. Visible keyboard focus on every interactive element.
`prefers-reduced-motion` respected. Contrast that passes on the render, not in
theory. These are not design decisions and they are never traded away for one.

## Words are design material

Copy makes a page feel as templated as its layout does. Name things by what the
person controls, not by how the system is built — someone manages notifications,
not webhook config. Active voice: "Save changes", not "Submit". An action keeps
its name through the whole flow, so a button that says "Publish" produces a
toast that says "Published". Errors say what happened and how to fix it, and
never apologise. An empty state is an invitation to act, not a shrug.

## Hand off

The brief goes to whoever implements, and they follow it exactly — every colour
and type decision derives from it. If implementation proves a decision wrong,
that comes back to this skill and the brief changes. The implementer does not
get to quietly pick a different blue.
