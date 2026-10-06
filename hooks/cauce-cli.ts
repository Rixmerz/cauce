// What the mod reads from cauce's answers, with no engine in reach: the engine
// interface is only ever used in the hooks module itself.
import type { CauceStop } from '../types'

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

/** What a resume button runs: the rules the person allows by pressing it, never
 * the model. Only where cauce says a resume continues it (`stop.next`). */
export function resumeArgs(card: { id: number; stop?: CauceStop | null }): string[] | null {
  const stop = card.stop
  if (!stop || !stop.next) return null
  const base = ['resume', String(card.id), '--detach']
  if (stop.cause === 'permission') {
    return stop.allow.length ? [...base, ...stop.allow.flatMap(rule => ['--allow', rule])] : null
  }
  if (stop.cause === 'approval') return [...base, '--allow-approval']
  if (stop.cause === 'budget' && stop.budget_usd) return [...base, '--budget', String(stop.budget_usd * 2)]
  return base
}

/** The same press, the rules also kept for every task in the task's repository:
 * one press per program, never again for the next task there. Only for a
 * refusal with rules to keep. */
export function keepArgs(card: { id: number; stop?: CauceStop | null }): string[] | null {
  const args = card.stop?.cause === 'permission' ? resumeArgs(card) : null
  return args ? [...args, '--keep'] : null
}

export function resumeLabel(cause: string): string {
  if (cause === 'permission') return 'Allow & resume'
  if (cause === 'approval') return 'Approve & resume'
  return 'Resume'
}
