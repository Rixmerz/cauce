# What was already tried against a problem

**Memory.** Before debugging, `locks memory search "<symptom>"` shows fixes
already tried against similar problems, in any repository. When a fix is
confirmed or refuted outside a worker, record it:
`locks memory record --problem "<title>" --fix "<what changed>" --outcome worked|failed --why "<why>"`.
