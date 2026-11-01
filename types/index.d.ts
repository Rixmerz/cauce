// What the locks mod reads from `locks board --full` for its status entry. Read from `locks board --full`, never
// written by the model.

export type LocksStop = {
  cause: string
  who: string
  reason: string
  todo: string
  next: string | null
  allow: string[]
  denied?: string[]
}

export type LocksCard = {
  id: number
  title: string
  status: string
  asks?: string
  stop?: LocksStop | null
  current_cell?: string | null
  branch?: string | null
  phase?: string | null
  /** Why a running task is slow (longer than 9 in 10 of its kind's passes here), when it is. */
  slow?: string | null
  /** A running task's progress in one line: stage or phase, time against its kind's usual here,
   * what it wrote so far, a worker gone quiet (`locks.progress`). */
  progress_line?: string | null
  files_line?: string | null
  /** Who clears a card that waits: the person, or the orchestrating session. */
  waits_on?: 'person' | 'orchestrator'
}

export type LocksBoard = {
  counts: { needs_you: number; orchestrator?: number; mine?: number; running: number; queued: number }
  needs_you: LocksCard[]
  running: LocksCard[]
  queued: { repo: string; paused: boolean; reason: string | null; tasks: LocksCard[] }[]
}

export type LocksNote = {
  id: number
  topic: string
  title: string
  text: string
  state: string
  state_reason: string | null
}

declare module 'claude-code' {
  interface PluginState {
    locks: {
      board: LocksBoard | null
      review: LocksNote[]
      hidden: string
    }
  }
}
