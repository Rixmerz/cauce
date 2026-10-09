// cauce as a Claude Code mod: the same core, reached from inside the session.
//
// - Tools the model calls by name (recall, note, queue, tasks, resume, overview) instead
//   of `cauce` through Bash: no PATH to find, no permission rule per command.
//   Their schemas are loose on purpose: a value outside a list is read by the
//   handler and answered with what cauce takes, never refused as "Invalid tool
//   parameters", which tells the model nothing.
// - Work a session queued that ends while it is idle wakes it: a turn starts
//   with the notice, and the session tells the person and goes on.
// - A status entry that counts what waits on the person, runs and is queued,
//   and `/cauce`, which opens the web board: the one place to look and press
//   (resume, allow, dismiss, unpause). A terminal copy of it was a poorer one.
//
// The classic hooks keep working without the mod: endings still reach the
// session at the end of a turn or with the next prompt, and the model can
// still run `cauce` through Bash. Nothing here grants a permission on the
// model's behalf: a refused command is allowed by the person's own press.
import type { EngineInterface, Register } from 'claude-code'

import type { CauceBoard } from '../types'
import type { Ran } from './cauce-cli'
import { DEFAULT_TOPICS, json, statusLine } from './cauce-cli'

const BOARD_URL = 'http://127.0.0.1:8790/'
const POLL_MS = 15_000
const KINDS = ['chat', 'explore', 'docs', 'test', 'refactor', 'implement', 'ui', 'feature', 'debug-repro',
  'debug-unclear', 'review-routine', 'review-critical', 'plan']
const LINKS = ['depends_on', 'explains', 'replaces', 'contradicts', 'example_of']


const RECALL = 'mcp__cauce__recall'
const NOTE = 'mcp__cauce__note'
const QUEUE = 'mcp__cauce__queue'
const TASKS = 'mcp__cauce__tasks'
const RESUME = 'mcp__cauce__resume'
const OVERVIEW = 'mcp__cauce__overview'
/** The tools that only read the project's notes and board, every session's
 * work, or keep a note in cauce's own store: asking the person for each is a
 * prompt that protects nothing. `queue` and `resume` spend money, and stay the
 * person's call. */
const QUIET = [RECALL, TASKS, NOTE, OVERVIEW]

/** A plugin tool's arguments, as the model gave them: loose until read. */
function input(e: object): Record<string, unknown> {
  return e as Record<string, unknown>
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/** A positive whole number, as a number or its digits ("42", "#42"); else null. */
function whole(value: unknown): number | null {
  const n = typeof value === 'number' ? value
    : typeof value === 'string' && /^#?\d+$/.test(value.trim()) ? Number(value.trim().replace('#', ''))
    : NaN
  return Number.isSafeInteger(n) && n > 0 ? n : null
}

// How the mod reaches cauce: the plugin's own launcher, run by argv. The core
// stays in Python; this layer only asks it and draws what it answers.

/** Run `cauce <args>` from the session's directory. Never rejects. */
async function cauce(
  $: EngineInterface,
  args: readonly string[],
  init: { session?: string; timeoutMs?: number } = {},
): Promise<Ran> {
  const bin = `${$.plugin.root}/bin/cauce`
  try {
    const ran = await $.process.run([bin, ...args], {
      // The session's id goes with the commands that act for it, so what they
      // queue or keep is that session's, as when it runs `cauce` through Bash.
      env: init.session ? { CAUCE_SESSION_ID: init.session } : {},
      timeoutMs: init.timeoutMs ?? 60_000,
    })
    return { ok: ran.exitCode === 0, out: ran.stdout.trim(), err: ran.stderr.trim(), code: ran.exitCode }
  } catch (error) {
    return { ok: false, out: '', err: `cauce did not run: ${String(error)}`, code: -1 }
  }
}

/** The board of one project, as `cauce board --full --repo` answers it. */
async function board($: EngineInterface, cwd: string): Promise<CauceBoard | null> {
  const full = json<CauceBoard>(await cauce($, ['board', '--full', '--repo', cwd]))
  return full && full.counts ? full : null
}

async function topics($: EngineInterface, cwd: string): Promise<string[]> {
  const found = json<{ name: string }[]>(await cauce($, ['notes', '--repo', cwd, 'topics', '--json']))
  const names = (found ?? []).map(t => t.name).filter(n => typeof n === 'string' && n.length > 0)
  return names.length ? names : [...DEFAULT_TOPICS]
}

// The module's own: started over on a reload, which is fine for both.
let busy = false
let polling = false

async function refresh($: EngineInterface): Promise<void> {
  const cwd = await $.session.cwd()
  const found = await board($, cwd)
  $.ui.status(statusLine(found?.counts))
}

/** Hand the session what ended while it was idle: claimed once, so the Stop
 * hook and the next prompt never deliver it again. */
async function wake($: EngineInterface, enabled: boolean): Promise<boolean> {
  if (!enabled || busy) return false
  const session = await $.session.id()
  const found = json<{ notice: string | null; ids: number[] }>(
    await cauce($, ['endings', '--session', session, '--json']))
  if (!found?.notice) return false
  $.ui.toast(`cauce: ${found.ids.map(id => `#${id}`).join(', ')} ended`)
  await $.prompt.submit({ text: found.notice })
  return true
}

async function poll($: EngineInterface, wakes: boolean): Promise<void> {
  if (polling) return
  polling = true
  try {
    await wake($, wakes)
    await refresh($)
  } finally {
    polling = false
  }
}

/** Work started and not awaited (a refresh after a call) never fails the hook that started it. */
function ignore(): void {}

/** A tool hook that threw answers for itself: the model reads why, never a hang. */
function failed(): { deny: string } {
  return { deny: 'cauce: the tool failed inside the mod; `cauce` through Bash still works' }
}

export const register: Register = (on, options) => {
  const wakes = options.wake !== false

  on('session.start', async ($, e, next) => {
    const answer = await next(e)
    const cwd = await $.session.cwd()
    const names = await topics($, cwd)
    await $.tool.register({
      name: 'recall',
      description: "Read what this project's notes say: business rules, how the code works, decisions and why, "
        + 'conventions, how it is run. Use it before rebuilding context from the code, above all after a '
        + 'compaction. A note marked TO REVIEW describes code that changed since: check it.',
      inputSchema: {
        type: 'object',
        properties: {
          question: { type: 'string', description: 'what you want to know; empty for the latest notes' },
          topic: { type: 'string', description: `only this topic: ${names.join(', ')}` },
          path: { type: 'string', description: 'notes about this file, relative to the checkout' },
        },
      },
    })
    await $.tool.register({
      name: 'note',
      description: "Keep a durable fact about this project in its notes, for whoever works here next: one the "
        + 'person told you or you confirmed. Not progress, not a guess, never a secret.',
      inputSchema: {
        type: 'object',
        properties: {
          fact: { type: 'string', description: 'one or two self-contained sentences, in the person\'s language' },
          topic: {
            type: 'string',
            description: `where it is filed: ${names.join(', ')}; left out, the topic its words point to`,
          },
          title: { type: 'string' },
          anchors: { type: 'array', items: { type: 'string' }, description: 'files it is about' },
          links: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                to: { type: ['integer', 'string'], description: 'the other note\'s id' },
                kind: { type: 'string', description: LINKS.join(', ') },
              },
            },
          },
        },
        required: ['fact'],
      },
    })
    await $.tool.register({
      name: 'queue',
      description: 'Hand a task to a cauce worker: it is classified, routed to a model and effort, run in its own '
        + 'worktree and checked. Returns at once; when it ends, its notice reaches this session on its own. '
        + 'Write the task so a stranger could do it: the worker sees nothing of this conversation.',
      inputSchema: {
        type: 'object',
        properties: {
          task: { type: 'string' },
          verify: { type: 'string', description: "the repository's own check, e.g. npm test" },
          kind: {
            type: 'string',
            description: `only when you know it: ${KINDS.join(', ')}; left out, cauce classifies the task`,
          },
        },
        required: ['task'],
      },
    })
    await $.tool.register({
      name: 'tasks',
      description: "This project's cauce work: what waits on the person and why, what runs, what is queued.",
      inputSchema: { type: 'object', properties: {} },
    })
    await $.tool.register({
      name: 'resume',
      description: 'Continue a cauce task that stopped, in the background, from its kept work. Refused when only '
        + 'the person can clear what stopped it (a refused command, an approval): tell them instead.',
      inputSchema: {
        type: 'object',
        properties: { id: { type: ['integer', 'string'] }, max_turns: { type: ['integer', 'string'] } },
        required: ['id'],
      },
    })
    await $.tool.register({
      name: 'overview',
      description: "Every Claude Code session cauce saw lately, in every project, and how its work stands: what "
        + 'waits on the person and why, what runs (attempt, model, since when), what is queued, what ended. For '
        + 'watching sessions in several projects from one place; it reads, and each session still hears its own endings.',
      inputSchema: {
        type: 'object',
        properties: { hours: { type: ['integer', 'string'], description: 'sessions seen and tasks ended this recently; 24 when left out' } },
      },
    })
    await $.command.register({ name: 'cauce', description: "cauce's board: what waits on you, runs and is queued" })
    $.clock.every(POLL_MS, () => {
      void poll($, wakes).catch(ignore)
    })
    void refresh($).catch(ignore)
    return answer
  })

  // A turn starts in the main conversation only; a subagent's turns end inside
  // it (`turn.complete` with an `agentId`) and leave it running.
  on('turn.start', ($, e, next) => {
    busy = true
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const answer = await next(e)
    if (e.agentId === undefined) {
      busy = false
      void refresh($).catch(ignore)
    }
    return answer
  })

  on('tool.call', { tool: RECALL }, async ($, call) => {
    const e = input(call)
    const args = ['recall', text(e.question), '--repo', await $.session.cwd()]
    if (text(e.topic)) args.push('--topic', text(e.topic))
    if (text(e.path)) args.push('--path', text(e.path))
    const ran = await cauce($, args)
    return ran.ok ? { result: ran.out || 'no notes match' } : { deny: ran.err || 'cauce recall failed' }
  }).catch(failed)

  on('tool.call', { tool: NOTE }, async ($, call) => {
    const e = input(call)
    const fact = text(e.fact)
    if (!fact) return { deny: 'a note needs a fact' }
    const args = ['note', fact, '--repo', await $.session.cwd(), '--json']
    // A topic is resolved by cauce, aliases included; one it does not know is
    // answered with the project's topics.
    if (text(e.topic)) args.push('--topic', text(e.topic))
    if (text(e.title)) args.push('--title', text(e.title))
    for (const path of Array.isArray(e.anchors) ? e.anchors : []) {
      if (text(path)) args.push('--anchor', text(path))
    }
    const skipped: string[] = []
    for (const link of Array.isArray(e.links) ? e.links : []) {
      const to = whole((link as { to?: unknown } | null)?.to)
      const kind = text((link as { kind?: unknown } | null)?.kind)
      if (to !== null && LINKS.includes(kind)) args.push('--link', `${to}:${kind}`)
      else skipped.push(JSON.stringify(link))
    }
    const ran = await cauce($, args, { session: await $.session.id() })
    const kept = json<{ id: number; new: boolean; topic: string; title: string }>(ran)
    if (!kept) return { deny: ran.err || 'cauce note failed' }
    const said = `${kept.new ? 'kept' : 'already known as'} #${kept.id} [${kept.topic}] ${kept.title}`
    return {
      result: skipped.length
        ? `${said}. Links left out (a link is {to: <note id>, kind: ${LINKS.join('|')}}): ${skipped.join(', ')}`
        : said,
    }
  }).catch(failed)

  on('tool.call', { tool: QUEUE }, async ($, call) => {
    const e = input(call)
    const task = text(e.task)
    if (task.length < 8) return { deny: 'write the task in full: the worker sees nothing of this conversation' }
    const args = ['queue', 'add', task, '--repo', await $.session.cwd(), '--json']
    if (text(e.verify)) args.push('--verify', text(e.verify))
    // A kind cauce does not have is left to its classifier, not a refusal: the
    // task is what matters, and the model hears what it named instead.
    const kind = text(e.kind)
    if (KINDS.includes(kind)) args.push('--kind', kind)
    const ran = await cauce($, args, { session: await $.session.id() })
    const queued = json<{ id: number; title: string; then: string }>(ran)
    if (!queued) return { deny: ran.err || 'cauce queue add failed' }
    void refresh($).catch(ignore)
    const unknown = kind && !KINDS.includes(kind)
      ? ` The kind "${kind}" is not one of cauce's (${KINDS.join(', ')}); it classifies the task itself.`
      : ''
    return {
      result: `queued #${queued.id} — ${queued.title}. ${queued.then} Its ending reaches this session on its own.${unknown}`,
    }
  }).catch(failed)

  on('tool.call', { tool: TASKS }, async $ => {
    const found = await board($, await $.session.cwd())
    if (!found) return { deny: 'cauce could not read its board' }
    const lines: string[] = []
    for (const card of found.needs_you) {
      lines.push(`waits on the person: #${card.id} [${card.status}] ${card.title}`)
      if (card.stop) {
        lines.push(`  stopped by ${card.stop.who}: ${card.stop.reason}`)
        lines.push(`  ${card.stop.todo}${card.stop.next ? `: ${card.stop.next}` : ''}`)
      } else if (card.asks) {
        lines.push(`  ${card.asks}`)
      }
    }
    for (const card of found.running) lines.push(`running: #${card.id} ${card.title}${card.current_cell ? ` (${card.current_cell})` : ''}`)
    for (const lane of found.queued) {
      for (const card of lane.tasks) lines.push(`queued: #${card.id} ${card.title}${lane.paused ? ` (lane paused: ${lane.reason ?? ''})` : ''}`)
    }
    return { result: lines.join('\n') || 'nothing waits, runs or is queued here' }
  }).catch(failed)

  on('tool.call', { tool: RESUME }, async ($, call) => {
    const e = input(call)
    const id = whole(e.id)
    if (id === null) return { deny: 'resume needs a task id: the number after # on the board (`tasks`)' }
    // `--unattended`: cauce refuses what only a person may clear. The model's
    // input names no rule; nothing it passes reaches `--allow`.
    const args = ['resume', String(id), '--unattended', '--detach']
    const turns = whole(e.max_turns)
    if (turns !== null) args.push('--max-turns', String(turns))
    const ran = await cauce($, args)
    if (!ran.ok) return { deny: ran.err || `cauce could not resume #${id}` }
    void refresh($).catch(ignore)
    return { result: ran.out }
  }).catch(failed)

  on('tool.call', { tool: OVERVIEW }, async ($, call) => {
    const hours = whole(input(call).hours)
    const ran = await cauce($, ['overview', '--hours', String(hours !== null && hours > 0 ? hours : 24)])
    return ran.ok ? { result: ran.out } : { deny: ran.err || 'cauce overview failed' }
  }).catch(failed)

  // Only an `ask` turns into an allow: a rule or a setting that denies one of
  // these still denies it, and an organization's ceiling still caps it.
  for (const tool of QUIET) {
    on('tool.check', { tool }, async ($, e, next) => {
      const verdict = await next(e)
      return verdict.decision === 'ask'
        ? { decision: 'allow', reason: "cauce: reads this project's notes and board, or keeps a note" }
        : verdict
    })
  }

  // The board is the web UI: one place to see and press, not a second, poorer
  // copy in the terminal. `cauce ui` serves until stopped, so it is started
  // detached; when one already serves, it says where and exits.
  on('command.run', { command: 'cauce' }, async $ => {
    const bin = `${$.plugin.root}/bin/cauce`
    try {
      await $.process.run(['/bin/sh', '-c', 'nohup "$0" ui --open >/dev/null 2>&1 &', bin], { timeoutMs: 10_000 })
    } catch (error) {
      return { text: `cauce: the board did not start (${String(error)}); run \`cauce ui --open\`` }
    }
    return { text: `cauce board: ${BOARD_URL}` }
  })
}
