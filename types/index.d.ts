// What the cauce mod reads from `cauce board --full` for its status entry. Read from `cauce board --full`, never
// written by the model.

export type CauceStop = {
  cause: string
  who: string
  reason: string
  todo: string
  next: string | null
  allow: string[]
  denied?: string[]
}

export type CauceCard = {
  id: number
  title: string
  status: string
  asks?: string
  stop?: CauceStop | null
  current_cell?: string | null
  branch?: string | null
  phase?: string | null
  /** Why a running task is slow (longer than 9 in 10 of its kind's passes here), when it is. */
  slow?: string | null
  /** A running task's progress in one line: stage or phase, time against its kind's usual here,
   * what it wrote so far, a worker gone quiet (`cauce.progress`). */
  progress_line?: string | null
  /** Who clears a card that waits: the person, or the orchestrating session. */
  waits_on?: 'person' | 'orchestrator'
}

export type CauceBoard = {
  counts: { needs_you: number; orchestrator?: number; mine?: number; running: number; queued: number }
  needs_you: CauceCard[]
  running: CauceCard[]
  queued: { repo: string; paused: boolean; reason: string | null; tasks: CauceCard[] }[]
}

export type CauceNote = {
  id: number
  topic: string
  title: string
  text: string
  state: string
  state_reason: string | null
}

declare module 'claude-code' {
  interface PluginState {
    cauce: {
      board: CauceBoard | null
      review: CauceNote[]
      hidden: string
    }
  }
}
