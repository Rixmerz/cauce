import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

type Run = { exitCode: number; stdout: string; stderr?: string }
type Answer = Run | ((argv: readonly string[]) => Run)

/** The world beneath the mod: a session, a fake `cauce`, and what the engine was asked. */
function world(on: On, answers: Record<string, Answer>, broken = false) {
  const seen = {
    argv: [] as string[][],
    env: [] as Record<string, string>[],
    tools: [] as { name: string; inputSchema?: Record<string, unknown> }[],
    commands: [] as string[],
    status: [] as (string | undefined)[],
    toasts: [] as string[],
    prompts: [] as string[],
    panes: [] as string[],
  }
  on('session.start', ($, e) => ({ cwd: e.cwd, startedAt: 0, rateLimits: [] }) as never)
  on('turn.start', ($, e) => ({ text: e.text, turnId: e.turnId, kind: 'engine', ref: 0 }) as never)
  on('turn.complete', ($, e) => ({ text: e.answer }) as never)
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.cwd', () => ({ value: '/work/shop' }))
  on('process.run', ($, e) => {
    if (broken) throw new Error('spawn ENOENT')
    const args = e.argv.slice(1)
    seen.argv.push(args)
    seen.env.push({ ...(e.init?.env ?? {}) })
    const key = Object.keys(answers).find(k => args.join(' ').startsWith(k))
    const found = key === undefined ? { exitCode: 0, stdout: '' } : answers[key]
    const ran = typeof found === 'function' ? found(args) : found!
    return { value: { exitCode: ran.exitCode, stdout: ran.stdout, stderr: ran.stderr ?? '', isStdoutTruncated: false,
      isStderrTruncated: false } }
  })
  on('tool.register', ($, e) => {
    seen.tools.push(e)
    return { value: { tool: `mcp__cauce__${e.name}` } }
  })
  on('command.register', ($, e) => {
    seen.commands.push(e.name)
    return { value: { command: e.name } }
  })
  on('ui.status', ($, e) => {
    seen.status.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', ($, e) => {
    seen.toasts.push(e.text)
    return { value: undefined }
  })
  on('ui.open', ($, e) => {
    seen.panes.push(e.id)
    return { value: { isPlaced: true } }
  })
  on('prompt.submit', ($, e) => {
    seen.prompts.push(e.text)
    return { text: e.text }
  })
  return seen
}

const EMPTY_BOARD = JSON.stringify({ counts: { needs_you: 0, running: 0, queued: 0 }, needs_you: [], running: [], queued: [] })

/** Every list in a schema, at any depth: none may be there, a value outside one is refused by the
 * engine as "Invalid tool parameters" before the mod can say what cauce takes. */
function enums(schema: unknown): string[] {
  if (!schema || typeof schema !== 'object') return []
  return Object.entries(schema as Record<string, unknown>)
    .flatMap(([key, value]) => (key === 'enum' ? [JSON.stringify(value)] : enums(value)))
}

test('the tools are registered with the project topics named, and no list the engine would refuse by', async ($, on) => {
  mock.clock(on)
  const seen = world(on, {
    'notes --repo /work/shop topics --json': { exitCode: 0, stdout: JSON.stringify([{ name: 'business' }, { name: 'billing' }]) },
    'board': { exitCode: 0, stdout: EMPTY_BOARD },
  })
  await $.session.start({ cwd: '/work/shop', surface: 'terminal', isInteractive: true })
  expect(seen.tools.map(t => t.name)).toEqual(['recall', 'note', 'queue', 'tasks', 'resume'])
  const note = seen.tools.find(t => t.name === 'note')!
  expect((note.inputSchema as any).properties.topic.description).toContain('business, billing;')
  expect((note.inputSchema as any).required).toEqual(['fact'])
  expect(seen.tools.flatMap(t => enums(t.inputSchema))).toEqual([])
  expect(seen.commands).toEqual(['cauce'])
})

async function started($: any, on: On, answers: Record<string, Answer>, options?: { wake?: boolean }) {
  const clock = mock.clock(on)
  const seen = world(on, { board: { exitCode: 0, stdout: EMPTY_BOARD }, ...answers })
  await $.session.start({ cwd: '/work/shop', surface: 'terminal', isInteractive: true })
  return { seen, clock }
}

function call($: any, tool: string, args: Record<string, unknown>) {
  return $.tool.call({ tool: `mcp__cauce__${tool}`, ...args })
}

// --- tools ---------------------------------------------------------------------------

test('without the project topics the five default ones are named', async ($, on) => {
  const { seen } = await started($, on, { 'notes --repo /work/shop topics': { exitCode: 1, stdout: '', stderr: 'boom' } })
  const note = seen.tools.find(t => t.name === 'note')!
  expect((note.inputSchema as any).properties.topic.description)
    .toContain('business, code, decisions, conventions, environment')
})

test('a topics answer that is not JSON falls back too', async ($, on) => {
  const { seen } = await started($, on, { 'notes --repo /work/shop topics': { exitCode: 0, stdout: 'not json' } })
  expect((seen.tools.find(t => t.name === 'recall')!.inputSchema as any).properties.topic.description)
    .toBe('only this topic: business, code, decisions, conventions, environment')
})

test('note passes the fact, the session and only well-formed links, and says which it left out', async ($, on) => {
  const { seen } = await started($, on, {
    note: { exitCode: 0, stdout: JSON.stringify({ id: 7, new: true, topic: 'business', title: 'Cents' }) },
  })
  const answer = await call($, 'note', {
    fact: 'Prices are stored in cents', topic: 'business', anchors: ['cart.py', '', 3],
    links: [{ to: 2, kind: 'explains' }, { to: '#5', kind: 'replaces' }, { to: 'x', kind: 'explains' },
      { to: 3, kind: 'loves' }, null, { to: 0, kind: 'explains' }],
  })
  expect(answer.result).toStartWith('kept #7 [business] Cents. Links left out')
  expect(answer.result).toContain('{"to":3,"kind":"loves"}')
  expect(answer.result).toContain('null')
  expect(answer.result).toContain('depends_on|explains|replaces|contradicts|example_of')
  const i = seen.argv.findIndex(a => a[0] === 'note')
  expect(seen.argv[i]).toEqual(['note', 'Prices are stored in cents', '--repo', '/work/shop', '--json',
    '--topic', 'business', '--anchor', 'cart.py', '--link', '2:explains', '--link', '5:replaces'])
  expect(seen.env[i]).toEqual({ CAUCE_SESSION_ID: 'sess-1' })
})

test('a note without a topic is filed by its words; any topic, an alias included, goes to cauce', async ($, on) => {
  const { seen } = await started($, on, {
    note: { exitCode: 0, stdout: JSON.stringify({ id: 8, new: true, topic: 'decisions', title: 'Why' }) },
  })
  expect((await call($, 'note', { fact: 'We chose SQLite because it ships with Python' })).result)
    .toBe('kept #8 [decisions] Why')
  expect(seen.argv.filter(a => a[0] === 'note')[0]).not.toContain('--topic')
  await call($, 'note', { fact: 'Discounts never stack', topic: 'negocio' })
  expect(seen.argv.filter(a => a[0] === 'note')[1]).toContain('negocio')
})

test("a topic cauce does not know comes back with the project's topics", async ($, on) => {
  await started($, on, {
    note: { exitCode: 2, stdout: '', stderr: "cauce: unknown topic 'misc'; known: business, code" },
  })
  const answer = await call($, 'note', { fact: 'Discounts never stack', topic: 'misc' })
  expect(answer.deny).toBe("cauce: unknown topic 'misc'; known: business, code")
})

test('an empty fact is refused without running cauce', async ($, on) => {
  const { seen } = await started($, on, {})
  const answer = await call($, 'note', { fact: '   ', topic: 'code' })
  expect(answer.deny).toBe('a note needs a fact')
  expect(seen.argv.some(a => a[0] === 'note')).toBe(false)
})

test("cauce's own refusal reaches the model, a secret included", async ($, on) => {
  await started($, on, {
    note: { exitCode: 2, stdout: '', stderr: 'cauce: a note needs a fact of a few words, and never a secret' },
  })
  const answer = await call($, 'note', { fact: 'the key is sk-abcdefghijklmnop', topic: 'code' })
  expect(answer.deny).toContain('never a secret')
})

test('a note answer that is not JSON is a refusal, not a silent pass', async ($, on) => {
  await started($, on, { note: { exitCode: 0, stdout: 'kept #1 garbled' } })
  const answer = await call($, 'note', { fact: 'A fact long enough', topic: 'code' })
  expect(answer.deny).toBe('cauce note failed')
})

test('recall builds its question, topic and path, and says when nothing matches', async ($, on) => {
  const { seen } = await started($, on, { recall: { exitCode: 0, stdout: '' } })
  const answer = await call($, 'recall', { question: 'discounts', topic: 'business', path: 'cart.py' })
  expect(answer.result).toBe('no notes match')
  expect(seen.argv.find(a => a[0] === 'recall')).toEqual(['recall', 'discounts', '--repo', '/work/shop', '--topic',
    'business', '--path', 'cart.py'])
  const bare = await call($, 'recall', {})
  expect(bare.result).toBe('no notes match')
})

test('a recall that fails is a refusal with the reason', async ($, on) => {
  await started($, on, { recall: { exitCode: 2, stdout: '', stderr: "cauce: unknown topic 'x'" } })
  expect((await call($, 'recall', { question: 'q', topic: 'x' })).deny).toContain('unknown topic')
})

test('queue refuses a task too short to be a brief, and runs nothing', async ($, on) => {
  const { seen } = await started($, on, {})
  for (const task of ['', 'fix', '  do it ']) {
    expect((await call($, 'queue', { task })).deny).toContain('write the task in full')
  }
  expect(seen.argv.some(a => a[0] === 'queue')).toBe(false)
})

test('queue passes the check, a known kind only, and the session', async ($, on) => {
  const { seen } = await started($, on, {
    'queue add': { exitCode: 0, stdout: JSON.stringify({ id: 4, title: 'Add the discount', then: 'A worker takes it now.' }) },
  })
  const answer = await call($, 'queue', { task: 'Add the volume discount to cart.total', verify: 'npm test', kind: 'hack' })
  expect(answer.result).toContain('queued #4 — Add the discount. A worker takes it now.')
  // a kind it does not have queues the task anyway, and the model hears why it was dropped
  expect(answer.result).toContain('The kind "hack" is not one of cauce\'s')
  const i = seen.argv.findIndex(a => a[0] === 'queue')
  expect(seen.argv[i]).toEqual(['queue', 'add', 'Add the volume discount to cart.total', '--repo', '/work/shop',
    '--json', '--verify', 'npm test'])
  expect(seen.env[i]).toEqual({ CAUCE_SESSION_ID: 'sess-1' })
  const known = await call($, 'queue', { task: 'Reproduce the double charge on retry', kind: 'debug-repro', verify: 7 })
  expect(known.result).not.toContain('not one of')
  expect(seen.argv.filter(a => a[0] === 'queue')[1]).toEqual(['queue', 'add', 'Reproduce the double charge on retry',
    '--repo', '/work/shop', '--json', '--kind', 'debug-repro'])
})

test('resume never carries a rule or an approval the model asks for', async ($, on) => {
  const { seen } = await started($, on, { resume: { exitCode: 0, stdout: 'resuming #12 in the background' } })
  const answer = await call($, 'resume', { id: 12, allow: ['Bash(*)'], allow_approval: true, max_turns: 'lots' })
  expect(answer.result).toContain('resuming #12')
  expect(seen.argv.find(a => a[0] === 'resume')).toEqual(['resume', '12', '--unattended', '--detach'])
})

test('resume takes an id and turns written as digits, as a model often sends them', async ($, on) => {
  const { seen } = await started($, on, { resume: { exitCode: 0, stdout: 'resuming in the background' } })
  await call($, 'resume', { id: '42', max_turns: '120' })
  await call($, 'resume', { id: ' #42 ', max_turns: '-5' })
  expect(seen.argv.filter(a => a[0] === 'resume')).toEqual([
    ['resume', '42', '--unattended', '--detach', '--max-turns', '120'],
    ['resume', '42', '--unattended', '--detach'],
  ])
})

test('resume: what only the person can clear comes back as a refusal', async ($, on) => {
  await started($, on, { resume: { exitCode: 3, stdout: '', stderr: 'task #12 was refused npm test: only the person can allow Bash(npm test:*)' } })
  expect((await call($, 'resume', { id: 12, max_turns: 150 })).deny).toContain('only the person can allow')
})

test('resume refuses an id that is no id, before running anything', async ($, on) => {
  const { seen } = await started($, on, {})
  for (const id of ['abc', -1, 0, 2.5, undefined, '1e3', '12abc', '', null, [12], 2 ** 60]) {
    expect((await call($, 'resume', { id })).deny).toContain('resume needs a task id')
  }
  expect(seen.argv.some(a => a[0] === 'resume')).toBe(false)
})

test('the reading tools are not put to the person; queue, resume and a deny are left as they are', async ($, on) => {
  let base: 'ask' | 'deny' | 'allow' = 'ask'
  on('tool.check', () => ({ decision: base, reason: 'the engine' }))
  await started($, on, {})
  const verdict = (name: string) => $.tool.check({ tool: `mcp__cauce__${name}`, input: {} })
  for (const name of ['recall', 'tasks', 'note']) {
    expect((await verdict(name)).decision).toBe('allow')
  }
  // queue and resume spend money: the person's rules and mode decide them
  expect((await verdict('queue')).decision).toBe('ask')
  expect((await verdict('resume')).decision).toBe('ask')
  // another tool, even one named like ours, is never touched
  expect((await $.tool.check({ tool: 'mcp__other__recall', input: {} })).decision).toBe('ask')
  expect((await $.tool.check({ tool: 'Bash', input: { command: 'cauce recall x' } })).decision).toBe('ask')
  // a deny stays a deny
  base = 'deny'
  for (const name of ['recall', 'tasks', 'note']) {
    expect((await verdict(name)).decision).toBe('deny')
  }
})

test('a cauce that cannot run is said, never a hang', async ($, on) => {
  mock.clock(on)
  const seen = world(on, {}, true)
  await $.session.start({ cwd: '/work/shop', surface: 'terminal', isInteractive: true })
  expect(seen.tools.length).toBe(5)
  expect((await call($, 'tasks', {})).deny).toBe('cauce could not read its board')
  expect((await call($, 'recall', { question: 'x' })).deny).toContain('cauce did not run')
})

const BOARD = {
  counts: { needs_you: 3, running: 1, queued: 1 },
  needs_you: [
    { id: 12, title: 'make npm test pass', status: 'blocked', stop: { cause: 'permission', who: 'your permission settings',
      reason: 'the worker was refused Bash(npm test)', todo: 'allow it', next: "cauce resume 12 --allow 'Bash(npm test:*)'",
      allow: ['Bash(npm test:*)'] } },
    { id: 13, title: 'reduce the app', status: 'replan', stop: { cause: 'spec', who: "the worker's verdict",
      reason: 'the API has no such field', todo: 'rewrite it', next: null, allow: [] } },
    { id: 9, title: 'docs', status: 'done', branch: 'cauce/task-9', asks: 'review branch cauce/task-9, then merge it' },
  ],
  running: [{ id: 14, title: 'write stats', status: 'running', current_cell: 'sonnet/medium' }],
  queued: [{ repo: 'r', paused: false, reason: null, tasks: [{ id: 15, title: 'add median', status: 'queued' }] }],
}

test('tasks says what waits on the person and how it goes on', async ($, on) => {
  await started($, on, { board: { exitCode: 0, stdout: JSON.stringify(BOARD) } })
  const answer = await call($, 'tasks', {})
  expect(answer.result).toContain('waits on the person: #12 [blocked] make npm test pass')
  expect(answer.result).toContain("allow it: cauce resume 12 --allow 'Bash(npm test:*)'")
  expect(answer.result).toContain('review branch cauce/task-9, then merge it')
  expect(answer.result).toContain('running: #14 write stats (sonnet/medium)')
  expect(answer.result).toContain('queued: #15 add median')
})

// --- waking the session -------------------------------------------------------------------

const NOTICE = 'cauce: work this session sent has ended since you last heard.\n#21 [done] add median'

test('what ended while the session was idle starts a turn, once', async ($, on) => {
  let calls = 0
  const { seen, clock } = await started($, on, {
    endings: () => ({ exitCode: 0, stdout: JSON.stringify(calls++ === 0 ? { notice: NOTICE, ids: [21] } : { notice: null, ids: [] }) }),
  })
  await clock.advance(15_000)
  await clock.advance(15_000)
  expect(seen.prompts).toEqual([NOTICE])
  expect(seen.toasts).toEqual(['cauce: #21 ended'])
  expect(seen.argv.filter(a => a[0] === 'endings')[0]).toEqual(['endings', '--session', 'sess-1', '--json'])
})

test('nothing ended: no turn, no toast', async ($, on) => {
  const { seen, clock } = await started($, on, { endings: { exitCode: 0, stdout: JSON.stringify({ notice: null, ids: [] }) } })
  await clock.advance(45_000)
  expect(seen.prompts).toEqual([])
  expect(seen.toasts).toEqual([])
})

test('while a turn runs, endings are left for the Stop hook', async ($, on) => {
  const { seen, clock } = await started($, on, { endings: { exitCode: 0, stdout: JSON.stringify({ notice: NOTICE, ids: [21] }) } })
  await $.turn.start({ text: 'go', turnId: 't1' } as never)
  await clock.advance(30_000)
  expect(seen.argv.some(a => a[0] === 'endings')).toBe(false)
  expect(seen.prompts).toEqual([])
  await $.turn.complete({ answer: 'done', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' } as never)
  await clock.advance(15_000)
  expect(seen.prompts).toEqual([NOTICE])
  // a subagent's turn inside the main one does not leave the session marked busy
  // a subagent's turn ending inside a main one leaves the session busy
  await $.turn.start({ text: 'again', turnId: 't2' } as never)
  await $.turn.complete({ answer: 'sub', durationMs: 1, isAborted: false, turnId: 't3', agentId: 'a1', reason: 'answer' } as never)
  const asked = seen.argv.filter(a => a[0] === 'endings').length
  await clock.advance(30_000)
  expect(seen.argv.filter(a => a[0] === 'endings').length).toBe(asked)
})

test('with wake off the session is never woken', { options: { wake: false } }, async ($, on) => {
  const { seen, clock } = await started($, on, { endings: { exitCode: 0, stdout: JSON.stringify({ notice: NOTICE, ids: [21] }) } })
  await clock.advance(60_000)
  expect(seen.argv.some(a => a[0] === 'endings')).toBe(false)
  expect(seen.prompts).toEqual([])
})

test('an endings answer that fails or is garbled wakes nothing', async ($, on) => {
  let n = 0
  const { seen, clock } = await started($, on, {
    endings: () => (n++ === 0 ? { exitCode: 1, stdout: '', stderr: 'locked' } : { exitCode: 0, stdout: '{"notice": ' }),
  })
  await clock.advance(30_000)
  expect(seen.prompts).toEqual([])
})

// --- the status entry ---------------------------------------------------------------------

test('the status entry counts the board and clears when it is empty', async ($, on) => {
  let board = JSON.stringify(BOARD)
  const { seen, clock } = await started($, on, { board: () => ({ exitCode: 0, stdout: board }) })
  await clock.advance(15_000)
  expect(seen.status.at(-1)).toBe('cauce · 3 waits on you · 1 running · 1 queued')
  board = EMPTY_BOARD
  await clock.advance(15_000)
  expect(seen.status.at(-1)).toBe(undefined)
  board = 'not json'
  await clock.advance(15_000)
  expect(seen.status.at(-1)).toBe(undefined)
})

// --- /cauce -------------------------------------------------------------------------------

test('/cauce starts the web board detached and says where it is; no pane, no band', async ($, on) => {
  const { seen } = await started($, on, { board: { exitCode: 0, stdout: JSON.stringify(BOARD) } })
  const ran = await $.command.run({ command: 'cauce', args: '' } as never)
  expect(ran.text).toBe('cauce board: http://127.0.0.1:8790/')
  expect(seen.panes).toHaveLength(0)
  const spawned = seen.argv.find(args => args[0] === '-c')
  expect(spawned?.[1]).toContain('ui --open')
})
