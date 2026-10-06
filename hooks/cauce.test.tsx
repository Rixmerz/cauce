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

test('the tools are registered with the project topics as the only choices', async ($, on) => {
  mock.clock(on)
  const seen = world(on, {
    'notes --repo /work/shop topics --json': { exitCode: 0, stdout: JSON.stringify([{ name: 'business' }, { name: 'billing' }]) },
    'board': { exitCode: 0, stdout: EMPTY_BOARD },
  })
  await $.session.start({ cwd: '/work/shop', surface: 'terminal', isInteractive: true })
  expect(seen.tools.map(t => t.name)).toEqual(['recall', 'note', 'queue', 'tasks', 'resume'])
  const note = seen.tools.find(t => t.name === 'note')!
  expect((note.inputSchema as any).properties.topic.enum).toEqual(['business', 'billing'])
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

test('without the project topics the five default ones are the choices', async ($, on) => {
  const { seen } = await started($, on, { 'notes --repo /work/shop topics': { exitCode: 1, stdout: '', stderr: 'boom' } })
  const note = seen.tools.find(t => t.name === 'note')!
  expect((note.inputSchema as any).properties.topic.enum).toEqual(['business', 'code', 'decisions', 'conventions', 'environment'])
})

test('a topics answer that is not JSON falls back too', async ($, on) => {
  const { seen } = await started($, on, { 'notes --repo /work/shop topics': { exitCode: 0, stdout: 'not json' } })
  expect((seen.tools.find(t => t.name === 'recall')!.inputSchema as any).properties.topic.enum.length).toBe(5)
})

test('note passes the fact, the session and only well-formed links', async ($, on) => {
  const { seen } = await started($, on, {
    note: { exitCode: 0, stdout: JSON.stringify({ id: 7, new: true, topic: 'business', title: 'Cents' }) },
  })
  const answer = await call($, 'note', {
    fact: 'Prices are stored in cents', topic: 'business', anchors: ['cart.py', '', 3],
    links: [{ to: 2, kind: 'explains' }, { to: 'x', kind: 'explains' }, { to: 3, kind: 'loves' }],
  })
  expect(answer.result).toBe('kept #7 [business] Cents')
  const i = seen.argv.findIndex(a => a[0] === 'note')
  expect(seen.argv[i]).toEqual(['note', 'Prices are stored in cents', '--topic', 'business', '--repo', '/work/shop',
    '--json', '--anchor', 'cart.py', '--link', '2:explains'])
  expect(seen.env[i]).toEqual({ CAUCE_SESSION_ID: 'sess-1' })
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
  const i = seen.argv.findIndex(a => a[0] === 'queue')
  expect(seen.argv[i]).toEqual(['queue', 'add', 'Add the volume discount to cart.total', '--repo', '/work/shop',
    '--json', '--verify', 'npm test'])
  expect(seen.env[i]).toEqual({ CAUCE_SESSION_ID: 'sess-1' })
})

test('resume never carries a rule or an approval the model asks for', async ($, on) => {
  const { seen } = await started($, on, { resume: { exitCode: 0, stdout: 'resuming #12 in the background' } })
  const answer = await call($, 'resume', { id: 12, allow: ['Bash(*)'], allow_approval: true, max_turns: 'lots' })
  expect(answer.result).toContain('resuming #12')
  expect(seen.argv.find(a => a[0] === 'resume')).toEqual(['resume', '12', '--unattended', '--detach'])
})

test('resume: what only the person can clear comes back as a refusal', async ($, on) => {
  await started($, on, { resume: { exitCode: 3, stdout: '', stderr: 'task #12 was refused npm test: only the person can allow Bash(npm test:*)' } })
  expect((await call($, 'resume', { id: 12, max_turns: 150 })).deny).toContain('only the person can allow')
})

test('resume refuses an id that is no id, before running anything', async ($, on) => {
  const { seen } = await started($, on, {})
  for (const id of ['abc', -1, 0, 2.5, undefined]) {
    expect((await call($, 'resume', { id })).deny).toBe('resume needs a task id')
  }
  expect(seen.argv.some(a => a[0] === 'resume')).toBe(false)
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
  expect(seen.status.at(-1)).toBe('cauce ⚠3 ▶1 ⏸1')
  board = EMPTY_BOARD
  await clock.advance(15_000)
  expect(seen.status.at(-1)).toBe(undefined)
  board = 'not json'
  await clock.advance(15_000)
  expect(seen.status.at(-1)).toBe(undefined)
})

// --- the band above the prompt and the pane ----------------------------------------------

const BAND = { component: 'AbovePrompt', props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 100,
  scroll: { bodyRows: 9, offset: 0 }, view: {} } } as const

function engineBand(on: On) {
  // What the engine draws when the mod has nothing to say.
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text key="engine">engine band</Text>
  })
}

async function withBoard($: any, on: On, board: object, answers: Record<string, Answer> = {}) {
  engineBand(on)
  const r = await started($, on, { board: { exitCode: 0, stdout: JSON.stringify(board) }, ...answers })
  await r.clock.advance(15_000)
  return r
}

test('nothing waits: the band is the engine\'s own', async ($, on) => {
  await withBoard($, on, JSON.parse(EMPTY_BOARD))
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'cauce', surface, ...BAND } as never)
    expect(await ui.find({ text: 'engine band' })).toBeDefined()
    await ui.unmount()
  }
})

test('a refusal offers Allow & resume, and the press runs exactly the suggested rules', async ($, on) => {
  const { seen } = await withBoard($, on, BOARD, { resume: { exitCode: 0, stdout: 'resuming #12 in the background' } })
  const ui = await $.ui.mount({ plugin: 'cauce', surface: 'terminal', ...BAND } as never)
  expect((await ui.find({ key: 'resume-12' }))?.props.label).toBe('Allow & resume')
  expect(await ui.find({ key: 'resume-13' })).toBeUndefined() // a task found wrong is rewritten, not resumed
  expect(await ui.find({ key: 'card-9' })).toBeUndefined() // a branch to review is no stop
  await ui.press({ key: 'resume-12' })
  expect(seen.argv.find(a => a[0] === 'resume')).toEqual(['resume', '12', '--detach', '--allow', 'Bash(npm test:*)'])
  expect(seen.toasts).toContain('cauce: resuming #12')
})

test('a refusal with no rule to offer has no button; other stops resume as cauce says', async ($, on) => {
  const stopped = (id: number, cause: string, extra: object = {}) => ({ id, title: `t${id}`, status: 'blocked',
    stop: { cause, who: 'x', reason: `r${id}`, todo: 'do', next: `cauce resume ${id}`, allow: [], ...extra } })
  const board = { ...BOARD, needs_you: [stopped(1, 'permission'), stopped(2, 'environment'),
    stopped(3, 'approval'), stopped(4, 'budget', { budget_usd: 2.5 })] }
  const { seen } = await withBoard($, on, board)
  const ui = await $.ui.mount({ plugin: 'cauce', surface: 'terminal', ...BAND } as never)
  expect(await ui.find({ key: 'resume-1' })).toBeUndefined()
  expect((await ui.find({ key: 'resume-2' }))?.props.label).toBe('Resume')
  expect((await ui.find({ key: 'resume-3' }))?.props.label).toBe('Approve & resume')
  expect(await ui.find({ text: /and 1 more/ })).toBeDefined()
  await ui.press({ key: 'resume-3' })
  expect(seen.argv.find(a => a[0] === 'resume')).toEqual(['resume', '3', '--detach', '--allow-approval'])
})

test('a failed resume press says why', async ($, on) => {
  const { seen } = await withBoard($, on, BOARD, { resume: { exitCode: 1, stdout: '', stderr: 'task #12 is running' } })
  const ui = await $.ui.mount({ plugin: 'cauce', surface: 'terminal', ...BAND } as never)
  await ui.press({ key: 'resume-12' })
  expect(seen.toasts).toContain('cauce: task #12 is running')
})

test('a survey keeps the band; Hide holds until the list changes', async ($, on) => {
  let board = BOARD
  engineBand(on)
  const { clock } = await started($, on, { board: () => ({ exitCode: 0, stdout: JSON.stringify(board) }) })
  await clock.advance(15_000)
  const survey = await $.ui.mount({ plugin: 'cauce', surface: 'terminal', ...BAND,
    props: { ...BAND.props, hasSurvey: true } } as never)
  expect(await survey.find({ text: 'engine band' })).toBeDefined()
  await survey.unmount()
  const ui = await $.ui.mount({ plugin: 'cauce', surface: 'terminal', ...BAND } as never)
  await ui.press({ key: 'hide' })
  expect(await ui.find({ text: 'engine band' })).toBeDefined()
  board = { ...BOARD, needs_you: [...BOARD.needs_you, { id: 30, title: 'new', status: 'blocked',
    stop: { cause: 'environment', who: 'the environment', reason: 'docker is down', todo: 'fix it', next: 'cauce resume 30', allow: [] } }] } as never
  await clock.advance(15_000)
  expect(await ui.find({ key: 'card-30' })).toBeDefined()
})

test('/cauce opens the board; its buttons cancel and confirm notes, with the project named', async ($, on) => {
  const review = [{ id: 5, topic: 'code', title: 'cents', text: 'x', state: 'review', state_reason: 'cart.py changed' }]
  const { seen } = await withBoard($, on, BOARD, { 'notes --repo /work/shop list --review': { exitCode: 0, stdout: JSON.stringify(review) } })
  const ran = await $.command.run({ command: 'cauce', args: '' } as never)
  expect(ran.text).toBe('cauce board opened.')
  expect(seen.panes).toEqual(['cauce'])
  const ui = await $.ui.mount({ plugin: 'cauce', surface: 'desktop', component: 'Pane', requestId: 'cauce',
    props: { title: 'cauce', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { bodyRows: 30, offset: 0 } } } as never)
  expect(await ui.find({ text: /Waits on you \(3\)/ })).toBeDefined()
  expect(await ui.find({ text: /Notes to review \(1\)/ })).toBeDefined()
  await ui.press({ key: 'cancel-14' })
  await ui.press({ key: 'ok-5' })
  expect(seen.argv).toContainEqual(['cancel', '14'])
  expect(seen.argv).toContainEqual(['notes', '--repo', '/work/shop', 'ok', '5'])
})

test('the pane says so when there is no board', async ($, on) => {
  engineBand(on)
  await started($, on, { board: { exitCode: 1, stdout: '' } })
  const ui = await $.ui.mount({ plugin: 'cauce', surface: 'terminal', component: 'Pane', requestId: 'cauce',
    props: { title: 'cauce', isFocused: false, bodyColumns: 80, placement: 'inline', scroll: { bodyRows: 30, offset: 0 } } } as never)
  expect(await ui.find({ text: /no board for this directory/ })).toBeDefined()
})
