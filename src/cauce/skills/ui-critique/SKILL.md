---
name: ui-critique
description: Judge a user-facing flow or a built screen against what a shipped product needs rather than what the happy path proves — the full state set every data-backed screen can be in, and the passes that find the missing ones. Use when designing a flow, reviewing a UI before merge, or when a screen works in the demo and a task says it still does not feel finished (.html/.css/.scss/.tsx/.jsx/.vue/.svelte/.astro, and any flow brief).
---

# ui-critique

> Precedence: `engineering-baseline` settles conflicts. This skill decides
> **what a screen must handle**; `design-brief` decides what it looks like and
> `web-ui-rules` decides how the markup and CSS are written. Where the project
> already has a pattern for a state — its own empty view, its own error
> surface — that pattern outranks every row below. Find it and reuse it.

"It works" and "it shipped" are different claims, and the gap between them is
not taste. A screen built to its happy path is *correct* and *unfinished*: the
first person to open it before any data exists, on a slow connection, with a
permission they do not have, sees something nobody decided. That is the whole
distance between a demo and a product, and it is enumerable.

Enumerable is the point. This is not a request to make things nicer. It is a
list, and a screen either handles a row or says why the row cannot happen.

## Every state a data-backed screen can be in

| State | Trigger | What it owes the person |
|---|---|---|
| **Empty — first run** | nothing of this kind has ever existed here | the action that creates the first one. Never "No results found" — there is no result to find yet, and the two states are different screens |
| **Empty — filtered out** | a search or filter excludes everything | which filter did it, and a way to clear it without retyping |
| **Loading — first paint** | no data yet, nothing to keep | reserve the space the content will take. A spinner that collapses into a layout shift is worse than a skeleton |
| **Loading — refresh** | data is on screen and being replaced | keep the old data visible and readable. Blanking on refresh loses the person's place |
| **Partial** | some of it arrived, some failed | show what arrived, and name what did not. A silent partial is a lie with data in it |
| **Error — recoverable** | timeout, 500, a dropped connection | what happened, and the control that tries again. Not the status code |
| **Error — permanent** | 404, deleted, a link that expired | that it is gone, and the nearest thing they can still do |
| **Forbidden** | 401/403, wrong role, expired session | whether to sign in again or ask someone — those are different sentences and only one of them is true |
| **Stale / offline** | the connection dropped with data on screen | that what they are reading is old, and how old |
| **Too much** | ten thousand rows, a hundred tabs | the strategy — paginate, virtualise, summarise. "It's slow" is what a person sees when nobody chose one |
| **Too long** | a title of two hundred characters, a name with no spaces | where it truncates, and whether the whole value is still reachable |
| **In flight** | an action was taken and has not resolved | that it was received. An unacknowledged click gets clicked again |

A row that genuinely cannot happen on this screen is written down as
unreachable, with the reason. Absent and impossible are different, and only one
of them is a decision.

## The passes

Run them in this order. Each one finds what the previous cannot.

1. **Second day.** Walk the flow as someone who used it yesterday and has data
   in it. Most screens are designed empty and used full.
2. **Wrong data.** Null in every nullable field, the longest plausible string,
   zero rows, one row, a thousand. Read the type or the schema for what is
   actually nullable rather than trusting the mock.
3. **Dead end.** Every screen, every state: can they leave without the browser
   back button? An error state with no exit is the most common one.
4. **Keyboard only.** Reach every action with Tab and Enter, and watch the
   focus ring the whole way. Focus that vanishes into a modal, or lands behind
   a sticky header, is a trap you can see in ten seconds.
5. **Reversible.** Every destructive action: can it be undone, for how long,
   and does the screen say so? Where it cannot be undone, what stands between a
   person and it — and is that thing proportional to the damage?
6. **Interruption.** Close the tab mid-flow and come back. What was lost, and
   was losing it a decision?

## Copy is part of the state, not a follow-up

A state with no words is not designed. Write the sentence:

- Say what happened and what to do — "That email is already registered. Sign in
  instead." Not "Error 422", and not "Something went wrong".
- Never apologise, never blame the person, never name the internals. A stack
  frame, a table name, or a status code in a user-facing string is a leak
  (CWE-209) as well as bad copy.
- One action keeps one name through the whole flow: a button that says
  *Publish* produces a toast that says *Published*.
- An empty state is an invitation, not a shrug.

## What this skill does not do

It does not pick colours, type, or the signature element — `design-brief` does,
and a state table is not a visual direction. It does not tell you how to write
the markup or the CSS — `web-ui-rules` does. And it does not license a redesign:
a screen that handles every row above and looks plain has passed this skill and
failed a different one.

## Security — outranks every rule above

- Never put an internal detail into a user-facing state: stack traces, SQL,
  file paths, table or column names, or the raw body of an upstream error
  (CWE-209, information exposure through an error message).
- The **forbidden** state must not distinguish "this does not exist" from "this
  exists and is not yours" unless the person is allowed to know it does exist.
  A 404 and a 403 that differ tell an attacker which identifiers are real
  (CWE-204, observable response discrepancy).
- An **in-flight** state that disables the control is a usability decision, not
  an authorisation one. Anything destructive is re-checked on the server;
  a disabled button stops a second click, never a second request (CWE-602).
