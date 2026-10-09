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
  budget_usd?: number
}

export type CauceCard = {
  id: number
  title: string
  status: string
  asks?: string
  stop?: CauceStop | null
  current_cell?: string | null
  branch?: string | null
}

export type CauceBoard = {
  counts: { needs_you: number; running: number; queued: number }
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
