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
 * what is queued, in words; nothing when the board is empty. */
export function statusLine(
  c: { needs_you: number; orchestrator?: number; running: number; queued: number } | undefined,
  running: { phase?: string | null }[] = [],
): string | undefined {
  if (!c || !(c.needs_you || c.orchestrator || c.running || c.queued)) return undefined
  // What the running work is doing now: exploring, implementing, checking.
  const phases = [...new Set(running.map(r => r.phase).filter((p): p is string => !!p))]
  const doing = phases.length ? ` (${phases.join(', ')})` : ''
  const waiting = c.needs_you + (c.orchestrator ?? 0)
  const parts = [
    waiting ? `${MARK.deciding} ${waiting} for the orchestrator` : '',
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
  const each = ids.map(id => {
    const how = statuses?.[String(id)]
    return how && ENDED[how] ? `#${id} ${ENDED[how]}` : `#${id} ended`
  })
  return `cauce: ${each.join(' · ')}`
}

/** What `cauce compaction next` answers: what the mod does now that the session
 * is idle, once per decision, and what cauce found out about this build. `wait`:
 * a decision is held (a run of this session holds the checkout), so ask again. */
export type CompactionStep = {
  action: 'suggest' | 'submit' | null
  decision?: number
  line?: string
  instructions?: string
  said?: string
  wait?: boolean
}

/** The toast that hands the person the `/compact` line to paste. */
export function suggestToast(line: string): string {
  return `cauce: a good moment to compact (a break between tasks, the cache still warm). Paste this:\n${line}`
}

/** The toast that says the engine compacted on cauce's word: every action the mod takes is visible. */
export function compactedToast(decision: number): string {
  return `cauce: compacted (#${decision}): \`compact auto\` compacted between turns with cauce's /compact instructions`
}

/** The toast that says the mod ran `/compact` on cauce's word, in a session with no compaction between turns. */
export function commandToast(decision: number): string {
  return `cauce: compacting (#${decision}): \`compact auto\` ran /compact with cauce's instructions`
}

/** The toast that says a hook vetoed the compaction cauce asked for. */
export function skippedToast(decision: number, reason: string): string {
  return `cauce: the compaction cauce asked for (#${decision}) was skipped: ${reason}`
}
