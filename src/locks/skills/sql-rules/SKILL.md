---
name: sql-rules
description: SQL and schema-migration conventions — parameterized queries, reversible DDL, safe index creation, expand/contract for destructive changes. Use ONLY when writing or reviewing SQL, DDL, or a database migration (.sql files, migration directories, ORM migration classes); do NOT apply to any other language.
---

# SQL Rules

> Apply ONLY when the file under edit or review is SQL, DDL, or a database
> migration. If the current file is not one of those, do not use this skill.

Precedence: `engineering-baseline` settles conflicts. The project's existing
migration tool and naming always win over anything preferred here.

## DO — queries
- Parameterize every value. Bind parameters, never string interpolation, and
  never "it's an internal integer so it's fine"
- Qualify columns in any query touching more than one table
- List columns explicitly in `INSERT` and in `SELECT` for application code —
  `SELECT *` breaks silently when the schema changes
- Put a `WHERE` on every `UPDATE`/`DELETE` you write, and read it twice
- Use `LIMIT` on anything that could scan an unbounded table
- Prefer a single set-based statement over a loop issuing one query per row
- Use explicit `JOIN ... ON`, never comma joins with conditions in `WHERE`
- Compare against `NULL` with `IS NULL`/`IS NOT NULL` — `= NULL` is never true

## DO — migrations
- Ship a rollback with every forward migration, and test up → down → up
- One logical change per migration; never bundle unrelated DDL
- Make DDL idempotent where the engine allows (`IF NOT EXISTS`, `IF EXISTS`)
- Use expand/contract for anything destructive: add the new column, backfill,
  switch reads, switch writes, *then* drop the old one — across separate
  migrations, never one
- Backfill in bounded batches with a commit per batch; a single `UPDATE` over
  millions of rows holds locks long enough to take the service down
- Create indexes concurrently where the engine supports it
  (`CREATE INDEX CONCURRENTLY` on Postgres) so writes are not blocked
- Set explicit `NOT NULL` + default on new columns only when the engine can do
  it without a full table rewrite; otherwise add nullable, backfill, constrain
- State the expected row count and lock behaviour of every destructive step in
  the migration's comment header

## DON'T
- Don't interpolate user input into SQL under any circumstance
- Don't edit a migration that has already been applied anywhere — add a new one
- Don't apply schema changes by hand outside the migration system
- Don't drop or rename a column in the same deploy that stops using it —
  old application instances are still running during a rolling deploy
- Don't add a foreign key or `NOT NULL` constraint without first proving the
  existing data satisfies it
- Don't rely on implicit type coercion in comparisons or joins
- Don't leave a transaction open across application logic or a network call
- Don't use `TRUNCATE` in a migration expecting it to roll back — it is not
  transactional on every engine
- Don't index every column "just in case" — each index is a write cost; add
  one when a real query plan asks for it

## Security — outranks every rule above

`engineering-baseline` is the general floor and `security-baseline` says how to
name, rank, and triage what you find. These are the surface-specific footguns,
tagged with the CWE to cite when you report one:

- Every value is a bound parameter. Interpolation into SQL is the finding — an internal-looking integer is still user input one call up (CWE-89)
- Identifiers (table/column names) cannot be bound: validate them against an allowlist, never a string from the request (CWE-89)
- Grant the application role only the privileges it uses; migrations run under a separate, more privileged role (CWE-250)
- Never put credentials, tokens, or personal data in a migration, a seed file, or a fixture (CWE-798)
- Mask or omit personal data when copying production rows into a test dataset (CWE-359)
- Enable row-level security or an explicit tenant predicate on every multi-tenant table — a forgotten `WHERE tenant_id` is cross-tenant read (CWE-639)
