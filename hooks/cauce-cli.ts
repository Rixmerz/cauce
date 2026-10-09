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

/** The status entry: what waits on the person first, in words; nothing when
 * the board is empty. */
export function statusLine(c: { needs_you: number; running: number; queued: number } | undefined): string | undefined {
  if (!c || !(c.needs_you || c.running || c.queued)) return undefined
  const parts = [
    c.needs_you ? `${c.needs_you} waits on you` : '',
    c.running ? `${c.running} running` : '',
    c.queued ? `${c.queued} queued` : '',
  ].filter(Boolean)
  return `cauce · ${parts.join(' · ')}`
}
