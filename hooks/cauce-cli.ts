// What the mod reads from cauce's answers, with no engine in reach: the engine
// interface is only ever used in the hooks module itself.

export type Ran = { ok: boolean; out: string; err: string; code: number }


export function json<T>(ran: Ran): T | null {
  if (!ran.ok || !ran.out) return null
  try {
    return JSON.parse(ran.out) as T
  } catch {
    return null
  }
}

export const DEFAULT_TOPICS = ['business', 'code', 'decisions', 'conventions', 'environment'] as const

// A status line and a toast are plain text: their color is a marker that has
// it, the same tone the web board gives each state, always beside its words.
const MARK = { needs: '🟠', deciding: '🟣', running: '🔵', queued: '⚪', done: '✅', failed: '❌', stopped: '⚪' } as const

/** The status entry: what waits on the orchestrator (it decides, or asks the
 * person in its conversation: nothing waits on the person here), what runs and
 * what is queued, in words; nothing when the board is empty. With `mine`, the
 * waits this session sent come first, and the rest of the project's apart. */
/** How many running tasks the status line tells one by one; more are counted. */
export const SHOWN_RUNNING = 2

export function statusLine(
  c: { needs_you: number; orchestrator?: number; mine?: number; running: number; queued: number } | undefined,
  running: { id?: number; phase?: string | null; progress_line?: string | null }[] = [],
): string | undefined {
  if (!c || !(c.needs_you || c.orchestrator || c.running || c.queued)) return undefined
  // What the running work is doing now: each task's progress when few run, else their phases.
  const told = running.length <= SHOWN_RUNNING && running.every(r => r.id !== undefined && r.progress_line)
  const phases = [...new Set(running.map(r => r.phase).filter((p): p is string => !!p))]
  const doing = told ? ` (${running.map(r => `#${r.id} ${r.progress_line}`).join('; ')})`
    : phases.length ? ` (${phases.join(', ')})` : ''
  const waiting = c.needs_you + (c.orchestrator ?? 0)
  const mine = c.mine === undefined ? waiting : Math.min(c.mine, waiting)
  const rest = waiting - mine
  const parts = [
    mine ? `${MARK.deciding} ${mine} for the orchestrator` : '',
    rest ? `${rest} more in the project` : '',
    c.running ? `${MARK.running} ${c.running} running${doing}` : '',
    c.queued ? `${MARK.queued} ${c.queued} queued` : '',
  ].filter(Boolean)
  return `cauce · ${parts.join(' · ')}`
}

const ENDED: Record<string, string> = {
  done: `${MARK.done} passed`, blocked: `${MARK.needs} blocked`, needs_approval: `${MARK.needs} needs approval`,
  replan: `${MARK.needs} replan`, failed: `${MARK.failed} failed`, interrupted: `${MARK.failed} interrupted`,
  cancelled: `${MARK.stopped} cancelled`, dismissed: `${MARK.stopped} dismissed`,
}

/** The toast for work that ended: each task with how it ended, when cauce said. */
export function endedToast(ids: number[], statuses?: Record<string, string | null>): string {
  // Nothing ended, yet there is a notice: a lane holding this session's work paused.
  if (ids.length === 0) return 'cauce: a lane holding your queued work is paused'
  const each = ids.map(id => {
    const how = statuses?.[String(id)]
    return how && ENDED[how] ? `#${id} ${ENDED[how]}` : `#${id} ended`
  })
  return `cauce: ${each.join(' · ')}`
}

/** What `cauce compaction next` answers: what the mod does now that the session
 * is idle, once per decision, and what cauce found out about this build. `wait`:
 * a decision is held (a run of this session holds the checkout), or a compaction
 * with a prompt to follow it has not finished yet, so ask again. */
export type CompactionStep = {
  action: 'suggest' | 'submit' | 'advise' | 'continue' | null
  decision?: number
  line?: string
  instructions?: string
  said?: string
  wait?: boolean
  /** With `advise`: what the orchestrating session is told; it decides. */
  advice?: string
  /** With `continue`: the prompt the session left itself to go on with once its compaction finished. */
  text?: string
}

/** The toast that hands the person the `/compact` line to paste. */
export function suggestToast(line: string): string {
  return `cauce: a good moment to compact (a break between tasks, the cache still warm). Paste this:\n${line}`
}

/** The toast that says the engine compacted on cauce's word: every action the mod takes is visible. */
export function compactedToast(decision: number): string {
  return `cauce: compacted (#${decision}) between turns with cauce's /compact instructions`
}

/** The toast that says the mod ran `/compact` on cauce's word, in a session with no compaction between turns. */
export function commandToast(decision: number): string {
  return `cauce: compacting (#${decision}): ran /compact with cauce's instructions`
}

/** The toast that says a hook vetoed the compaction cauce asked for. */
export function skippedToast(decision: number, reason: string): string {
  return `cauce: the compaction cauce asked for (#${decision}) was skipped: ${reason}`
}
