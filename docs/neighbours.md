# What a neighbour tells cauce about itself

A neighbour (livespec, layout-inspector, rastro) knows its own tools better
than cauce does, and changes them more often than cauce is released. So a
plugin may say, in its own installed manifest, what a worker should be told of
it. cauce reads that when it plans a task (`src/cauce/manifests.py`): updating
the plugin changes what workers hear on the next task, with no cauce release
and no restart.

## Where it lives

In the installed plugin, newest version only:

```
<claude config>/plugins/cache/<marketplace>/<name>/<version>/.claude-plugin/plugin.json   # key "cauce"
<claude config>/plugins/cache/<marketplace>/<name>/<version>/.claude-plugin/cauce.json    # the whole file
```

The `cauce` key of `plugin.json` wins over `cauce.json`. A half-done `temp_*`
clone is never read, and neither is anything in a repository: a repository
(its `docs/tracer/` included) can never become instructions this way.

## Format 1

```json
{"format": 1,
 "tools":    {"<tool>": "what it is for"},
 "hint":     "the line a worker gets",
 "hints":    {"<kind>": "the line for this kind of work"},
 "measures": {"<tool>": "what it measures"}}
```

| Field | Used for | Built-in it replaces |
|---|---|---|
| `tools` | the tools the hint may name, and what each is for | `Neighbour.tools`, `livespec.TOOLS` |
| `hint` | the worker's line (and the verifier's, for one that measures) | `Neighbour.hint` |
| `hints` | livespec's line per kind of work (`implement`, `ui`, …); a kind not listed falls back to `hint`, then the built-in | `adapters/livespec._HINTS` |
| `measures` | what each tool measures, added to the TRACER method's lines in the independent check | `tracer.MEASURED_BY` text |

Rules:

- `format` must be the integer `1`. Another value, malformed JSON, or any
  field of the wrong type and the whole block is ignored: cauce uses its
  built-in text, never part of one and part of the other.
- A `hint` or `hints` entry that names a tool (a `snake_case` word or a word
  in backticks) the block's own `tools` lacks is dropped. A `measures` key not
  in `tools` is dropped.
- Every field is optional; an empty block changes nothing.

## Words, never policy

A manifest changes only what a worker or the verifier reads. These stay in
cauce's code whatever a manifest says:

- which servers a worker gets and when (`when`, `after_failure`, `touches`),
  and whether a neighbour measures (`Neighbour.measures`: who the independent
  verifier gets);
- the server command and the arguments cauce adds (`extra_args`, rastro's
  `--routines=off`);
- every `cauce.mcp_guard` policy (rastro's guard, livespec's read-only set).

A key the format does not know (`guard`, `extra_args`, …) is simply not read.

## Seen in the plan

The plan says, once a day per repository, which neighbours' words came from
their manifest and which version:

```
manifests: layout-inspector 0.5.0, livespec 0.35.0 told cauce how to use them (their plugin manifest); policy and every other neighbour stay cauce's own
```

## Examples

Drafts built from cauce's current built-in data, ready to copy into each
plugin's `.claude-plugin/plugin.json`:

- livespec: [`neighbours/livespec.json`](neighbours/livespec.json) (`tools` and `hints`)
- layout-inspector: [`neighbours/layout-inspector.json`](neighbours/layout-inspector.json) (`tools`, `hint` and `measures`)

A minimal rastro block:

```json
{"cauce": {"format": 1,
           "tools": {"rastro_open": "open a page", "rastro_view": "what can be acted on"},
           "hint": "open the app with rastro_open and read it with rastro_view"}}
```
