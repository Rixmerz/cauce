---
name: web-ui-rules
description: Framework-agnostic UI conventions — semantic HTML, accessibility, CSS layout and specificity, responsive and theme discipline. Use ONLY when the file under edit or review renders user interface — HTML, CSS/SCSS, or a component template (.html/.css/.scss/.jsx/.tsx/.vue/.svelte/.astro/.erb/.liquid); do NOT apply to non-UI code.
---

# Web UI Rules

> Apply ONLY when the file under edit or review renders UI — markup, styles, or
> a component template. The component's *language* rules (`typescript-rules` for
> `.tsx`, `ruby-rules` for `.erb`) still apply on top of this; this file covers
> what those do not: markup, styling, and accessibility.

Precedence: `engineering-baseline` settles conflicts. The project's existing
design system, utility framework, and component patterns win over anything
preferred here.

## DO — markup and accessibility
- Reach for the native element first: `button`, `a`, `label`, `details`,
  `dialog`, `input type=…`. Native before ARIA, ARIA before a styled `div`
- An `a` navigates, a `button` acts. Never a clickable `div`
- Every input has a programmatically associated `label`; placeholder is not one
- Every meaningful image has `alt`; every decorative one has `alt=""`
- Keep one `h1` per page and never skip heading levels for visual size
- Make every interactive element keyboard-reachable, in a sensible tab order,
  with a visible focus style — never `outline: none` without a replacement
- Announce async state changes with a live region, not colour alone
- Use `aria-*` only to describe state a native element cannot express, and keep
  it in sync with the actual state
- Use stable, unique keys for dynamic lists — never the array index for data
  that can reorder
- Select by role or accessible name in tests, never by CSS class

## DO — CSS
- Lay out with flexbox and grid; reserve absolute positioning for overlays
- Use logical properties (`margin-inline`, `padding-block`) over directional
- Use relative units (`rem`, `%`, `ch`, `fr`) for anything text-sized;
  reserve `px` for borders and hairlines
- Keep specificity flat — one class per rule; no ID selectors for styling
- Put colours, spacing, and radii in custom properties and consume the tokens
- Constrain wide content (tables, code, diagrams) with its own
  `overflow-x: auto` container so the page body never scrolls sideways
- Give images `max-width: 100%` and an intrinsic `width`/`height` (or
  `aspect-ratio`) so layout does not shift on load
- Define the light palette on `:root` and override tokens under
  `prefers-color-scheme: dark` — never leave a colour defined only inside a
  media query
- Respect `prefers-reduced-motion` for any non-trivial animation

## DO — composition

The rules above make CSS correct; correct CSS still looks generic. These are the
floor for a UI that reads as designed:

- Give one element the lead. Three or more items at equal visual weight is a
  grid with no focal point — vary size, weight, or space until something wins
- Keep a heading inside about three lines; past that it is a heading doing a
  paragraph's job, and the excess belongs in the copy below it
- Spend one accent colour, sparingly. A second accent competing with the first
  leaves the page with none
- Prefer an off-black (a near-black carrying a trace of the page's hue) to
  `#000` and an off-white to `#FFF`, unless the design commits to them on purpose
- Cap body text near `65ch`. A full-width paragraph is unreadable at any size
- Keep the type scale to 5-7 steps. An eighth size added by hand is how a
  design stops looking designed

The `design-brief` skill decides the palette, the type scale, and the layout.
This section is the floor those decisions have to clear, not a substitute for
making them.

## DON'T
- Don't put business logic — API calls, transformations, validation — in a
  component. Extract to a hook, store, or service
- Don't use `!important` to win a specificity fight; fix the selector
- Don't nest selectors more than two levels deep in SCSS
- Don't set a fixed `height` on anything containing text
- Don't convey meaning with colour alone — pair it with text or an icon
- Don't ship `tabindex` values above `0`
- Don't animate `width`/`height`/`top`/`left`; animate `transform` and `opacity`
- Don't add a UI dependency for something the platform already does
  (`dialog`, `details`, `input type=date`, CSS scroll-snap)
- Don't inline user-controlled HTML (`innerHTML`, `dangerouslySetInnerHTML`,
  `v-html`) without sanitizing — this is an XSS finding, not a style note

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- Never render user-controlled HTML through `innerHTML`, `dangerouslySetInnerHTML`, or `v-html` without sanitizing — the most common XSS sink in UI code (CWE-79)
- Never build a URL scheme from user input; `javascript:` and `data:` in an `href` execute (CWE-79)
- Escape user data into inline styles and attributes, not only into text nodes (CWE-79)
- Keep tokens out of `localStorage` where an XSS can read them (CWE-922) — prefer an httpOnly cookie
- Never put a secret, API key, or internal hostname in client-side code; anything shipped to the browser is public (CWE-798)
- Set `rel="noopener noreferrer"` on any `target="_blank"` link to an external origin (CWE-1022)
