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
const MARK = { needs: '🟠', running: '🔵', queued: '⚪', done: '✅', failed: '❌', stopped: '⚪' } as const

/** The status entry: what waits on the person first, in words; nothing when
 * the board is empty. */
export function statusLine(c: { needs_you: number; running: number; queued: number } | undefined): string | undefined {
  if (!c || !(c.needs_you || c.running || c.queued)) return undefined
  const parts = [
    c.needs_you ? `${MARK.needs} ${c.needs_you} waits on you` : '',
    c.running ? `${MARK.running} ${c.running} running` : '',
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
