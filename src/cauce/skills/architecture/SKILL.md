---
name: architecture
description: Software architecture reasoning — non-functional requirements, design-pattern selection, and scenario-based tradeoff evaluation (ATAM/SAAM essence). Use when designing a system, choosing a pattern, evaluating an architecture, or writing a design doc. Biased toward the simplest structure that meets the requirements.
---

# Architecture

> Design is the one thing you do NOT delegate. This skill arms the engineer's
> own thinking — vocabulary, checklists, and tradeoff frames — not a process to
> follow by rote. Every framework here is filtered through one rule: **reach for
> the simplest structure that satisfies the non-functional requirements, and no
> more.** Academic methods below describe the 20% worth keeping, not the ceremony.

## Prime directive

An architecture decision is justified only by a **concrete quality scenario**
it must satisfy — never by "best practice", résumé-driven design, or a diagram
that looks complete. No scenario demands it → don't build it (YAGNI at the
structural level). Monolith-first; distribute only when a named NFR forces it.

## Non-functional requirements (the real design drivers)

Functional requirements say what the system does; NFRs decide its shape. Before
choosing any structure, force each relevant NFR into a **measurable scenario**
("p99 < 200ms at 1k rps", not "fast"). Skip the ones no stakeholder actually
needs — an unowned NFR is speculative complexity.

| NFR | The question it forces | Cost if over-built |
|-----|------------------------|--------------------|
| Performance | Response time / throughput / resource budget under real load? | Premature caching, denormalization, async everywhere |
| Availability | Acceptable downtime? Blast radius of one failure? | Redundancy nobody's SLA needs |
| Scalability | Growth curve of users/data over 12–18mo — real or imagined? | Microservices for a 3-user app |
| Security | Trust boundaries, sensitive data, threat model? | Never over-built — under-built is the risk |
| Maintainability | Who changes this in a year? How fast? | Abstraction layers that hide, not help |
| Interoperability | Which external systems must it integrate with, via what contract? | Generic adapters for one integration |
| Portability | Which runtimes/devices/clouds are actually targeted? | Lowest-common-denominator paralysis |
| Compliance | Regulated domain (PCI/HIPAA/GDPR)? Which controls are mandatory? | Not over-built — a real constraint |
| Deployability | Release cadence, rollback story, migration safety? | Pipeline heavier than the app |
| Testability | Can each seam be verified at its entrypoint? | Interfaces existing only "for mocking" |

Rule: **name the scenario, name the owner, then choose the structure.** An NFR
with no owner and no number is not a requirement — it's an excuse to over-build.

## Design patterns — catalog and selection

Patterns are a shared vocabulary for structure, not a shopping list. The ponytail
ladder applies first: stdlib / language feature / one obvious class usually beats
a named pattern. Introduce a pattern only when duplication or a real axis of
change has already shown up — **twice**, not once.

**Creational** (control object creation): Singleton · Factory Method · Abstract
Factory · Builder · Prototype. Use when construction logic is non-trivial or must
vary. *Trap:* Singleton is a global — most "singletons" are just one instance you
pass in (DI). Reach for it last.

**Structural** (compose objects): Adapter · Bridge · Composite · Decorator ·
Facade · Flyweight · Proxy. Use to tame an interface mismatch or a growing object
graph. *Trap:* a Facade over a system with one caller is just indirection.

**Behavioral** (object interaction): Chain of Responsibility · Command · Iterator ·
Mediator · Observer · State · Strategy · Template Method · Visitor. Use when
control flow or responsibility assignment is the hard part. *Trap:* Strategy with
one strategy, Observer with one observer — delete the indirection.

### Selection procedure (each step can abort)

1. **Identify the concrete problem** — the specific pain, not "make it flexible".
2. **Evaluate plain solutions first** — does a function/stdlib/existing dep solve it? If yes, **stop** — no pattern.
3. **Match candidate patterns** to the problem's *shape* (creation? composition? interaction?).
4. **Verify the context fits** — the pattern's preconditions actually hold here.
5. **Weigh the consequences** — every pattern adds indirection; is the flexibility bought one you'll use *soon*?
6. **Implement and adapt** — the textbook form is a starting point, not a contract. Fit it to the codebase's idiom.

## Evaluating an architecture (ATAM/SAAM, the keepable 20%)

You rarely need the full academic ceremony. The durable idea from all of them is
**scenario-based, tradeoff-aware evaluation**: quality attributes trade against
each other, so surface the conflicts before committing.

- **Tradeoff analysis (ATAM essence):** list the driving quality scenarios, then
  for each candidate decision name what it *helps* and what it *hurts*. A decision
  that helps one NFR and hurts none is free — take it. One that trades NFR-A for
  NFR-B is a **sensitivity/tradeoff point** — that's where to spend judgment and
  flag risk. This is the whole value of ATAM without the workshops.
- **Modifiability probe (SAAM essence):** take the 2–3 most likely future changes
  and trace how many components each touches. Change that ripples across many
  modules = a seam in the wrong place. Cheaper to find on paper than in code.
- **Quality-attribute workshop (QAW essence):** align stakeholders on the ranked
  NFRs *before* designing. Solo/small-team version: write the ranked scenario list
  and confirm it — five minutes that prevent building for the wrong attribute.
- **FURPS+** is a checklist, not a method — use it only to avoid forgetting an NFR
  category (Functionality, Usability, Reliability, Performance, Supportability +
  design/impl/interface constraints).

Heavyweight methods (ARID, ALMA, MECABIC, Bosch, SonarQube/ArchiMate tooling)
exist for large regulated systems with dedicated architects. For everything else,
the four essences above are the leverage; invoking the full method is the
over-engineering this skill exists to prevent.

## Design-doc shape (when one is warranted)

Only for changes crossing a real boundary (new service, schema, integration,
security surface). Keep it to what forces a decision:

1. **Problem & driving NFRs** — the ranked, measurable scenarios.
2. **Options considered** — 2–3, each with its tradeoff points named.
3. **Decision** — the chosen option and the scenario that decided it.
4. **Consequences** — what this makes easy, what it makes hard, what it risks.

If the doc is longer than the change it justifies, the change didn't need a doc.

## DON'T

- Don't pick a pattern before proving a plain solution fails.
- Don't adopt microservices/event-sourcing/CQRS/K8s without a named NFR scenario that a monolith provably can't meet.
- Don't cite "Netflix/Google/Amazon does X" — their NFRs are not yours; scale is a requirement, not an aspiration.
- Don't add an abstraction for a variation that hasn't happened twice.
- Don't run a full ATAM/ALMA when the four essences answer the question.
- Don't confuse a diagram that looks complete with an architecture that's justified.
