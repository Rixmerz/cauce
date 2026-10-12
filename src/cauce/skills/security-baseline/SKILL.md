---
name: security-baseline
description: How to name, rank, and triage a security finding — CWE as the vocabulary, an evidence-anchored severity ladder instead of an invented CVSS score, the reachability-first protocol for a dependency CVE, and supply-chain discipline. Use when auditing code, triaging scanner output (SAST/SCA/secrets), reviewing a security-sensitive diff, deciding how urgent a vulnerable dependency is, or writing up findings.
---

# security-baseline

The per-language `*-rules` skills say what not to write. This says how to
**name** what you found, how to **rank** it, and what to do about a
vulnerability in something you did not write.

Precedence: this outranks every style rule. `engineering-baseline` rung 2.

## Name findings with CWE

A finding without a CWE is a sentence; with one it is a class. Two auditors
describing "unsafe query building" and "user input in SQL" are reporting one
defect, and only the ID makes that obvious. It also makes the finding
searchable, comparable across languages, and dedupable across a report.

Format: `CWE-89` inline, right after the claim. One CWE per finding — pick the
most specific one that fits, not the parent category.

**Never invent an ID.** If you are not confident which CWE applies, write the
finding without one and say so. A wrong CWE routes the fix to the wrong
reviewer and is worse than none.

### The surfaces worth checking, indexed by CWE

Walk this list against the change under review. It is the OWASP Top 10 in the
order a code reader meets it, with the CWEs that actually show up:

| Surface | Look for | CWE |
|---|---|---|
| Access control | endpoint/handler with no explicit authz; IDOR on an object ID from the request | CWE-862, CWE-639 |
| Authentication | missing authn, weak session invalidation, credential stuffing surface | CWE-306, CWE-613 |
| Injection | SQL, OS command, LDAP, template, code | CWE-89, CWE-78, CWE-94 |
| XSS | user data into HTML, attributes, inline styles, URL schemes | CWE-79 |
| Deserialization | pickle, `ObjectInputStream`, `BinaryFormatter`, `Marshal.load`, `unserialize` | CWE-502 |
| Path handling | user-controlled path joined without containment check | CWE-22 |
| SSRF | user-controlled URL fetched server-side | CWE-918 |
| Crypto | broken algorithm, weak password hash, non-constant-time compare | CWE-327, CWE-916, CWE-208 |
| Randomness | a non-CSPRNG generating a token, session ID, or nonce | CWE-330 |
| Secrets | hardcoded credential, secret in argv, secret in a log line | CWE-798, CWE-214, CWE-532 |
| XML | external entity resolution left enabled | CWE-611 |
| Memory (C/C++) | out-of-bounds write, use-after-free, integer overflow into an allocation | CWE-787, CWE-416, CWE-190 |
| Configuration | insecure default, verbose error exposing internals, permissive CORS | CWE-1188, CWE-209, CWE-942 |
| Dependencies | known-vulnerable version in the lockfile | see the CVE protocol below |
| Logging | security-relevant event not logged, or PII written to the log | CWE-778, CWE-359 |

## Rank findings by preconditions, not by a score you made up

**Do not compute a CVSS vector from reading code.** You cannot know the
deployment, the network exposure, or the data classification, and a fabricated
`7.5` carries more authority than the evidence behind it. If the project
already assigns CVSS, quote its number and its source; never derive one.

Rank on what an attacker needs, which is something you *can* read off the code:

| Severity | Preconditions |
|---|---|
| **critical** | Unauthenticated, remote, reaches data or code execution. No user interaction. |
| **high** | Authenticated-but-unprivileged reaches data or actions of another user; or unauthenticated with a realistic precondition (a known ID, a race). |
| **medium** | Requires privilege the attacker must already hold, or a precondition outside their control; or leaks information that enables another step. |
| **low** | Defense-in-depth. Nothing exploitable today; the next change makes it exploitable. |

State the precondition in the finding. `high — any logged-in user can read
another tenant's invoices by changing the ID (CWE-639)` is actionable;
`high severity IDOR` is not.

## A dependency CVE: reachability first

An SCA tool reports the version, not the risk. Most reported CVEs in a lockfile
are not reachable from the application, and treating them all as urgent is how
teams learn to ignore the scanner.

1. **Confirm it is real.** Quote the advisory ID (`CVE-…`, `GHSA-…`) and the
   affected/fixed version range from the tool's own output. Never assert a CVE
   from memory — advisory data changes and your training data is stale.
2. **Establish reachability.** Does the project call the vulnerable API path at
   all? Grep the entrypoints named in the advisory. A vulnerability in a code
   path the project never invokes is *medium at most*, and say why.
3. **Prefer the version bump.** A patched release is nearly always cheaper than
   a workaround, and workarounds rot.
4. **No fix available?** State the mitigation (input constraint, feature
   disabled, network boundary) and what makes it sufficient. "Accepted risk"
   without a mitigation is an unfixed finding with better wording.
5. **Transitive?** Say which direct dependency pulls it in — that is who has to
   move, and it is the part the report usually omits.

Run the project's SCA rather than reasoning about dependencies from the
manifest: `pip-audit`, `npm audit`, `cargo audit`, `govulncheck`,
`osv-scanner`. The one the repository already runs (its CI, its scripts) is
the one to use.

## Supply chain

- Lockfiles are committed, and a dependency change updates the lockfile in the
  same commit.
- Pin the way the project pins. Never add an unpinned dependency to a repo that
  pins (CWE-1104).
- Verify integrity on anything fetched during a build — checksum or signature,
  never a bare `curl | sh` (CWE-494).
- A new dependency is a new trust relationship: check that it is the package you
  think it is (name, publisher, download volume), not a typosquat.
- Build and CI credentials are secrets like any other — least privilege, and
  never echoed into a log.

## Reporting

- Every finding: `file:line` — severity — CWE — what an attacker can do — fix.
- Rank by exploitability, criticals first.
- **State what was NOT checked.** "No findings" and "no risk" are different
  claims, and only the first one is ever yours to make.
- No padding. An empty finding list with honest scope is a valid result; an
  invented nitpick to look thorough costs the reader's trust in the whole
  report.
