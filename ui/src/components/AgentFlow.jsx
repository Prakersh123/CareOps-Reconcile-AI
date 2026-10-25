import { useEffect, useMemo, useState } from 'react'

// A live picture of the agent system: which part is working now, and where the work just moved.
// Built from the same progress events as the timeline; each event becomes one step, played at a
// readable pace so a burst of events (one poll) is not a blur. A finished run can be replayed.

const W = 170
const H = 60
const NODES = {
  reviewer: { x: 10, y: 20, w: 130, title: 'Reviewer', sub: 'asks in plain English' },
  investigator: { x: 180, y: 20, title: 'Investigator Agent', sub: 'plans, writes findings' },
  data: { x: 430, y: 20, title: 'Data Agent', sub: 'writes read-only SQL' },
  zetaris: { x: 690, y: 20, title: 'Zetaris USL', sub: '3 sources, 1 table' },
  code: { x: 180, y: 120, title: 'Code checks', sub: 'evidence vs register' },
  world: { x: 430, y: 120, title: 'World Model', sub: 'Meterless register' },
  critic: { x: 180, y: 220, title: 'Critic Agent', sub: 'ACCEPT or RETRY' },
  report: { x: 690, y: 220, title: 'Report', sub: 'for human review' },
}

const EDGES = {
  ask: 'M140 50 H180',
  'inv-data': 'M350 42 H430',
  'data-inv': 'M430 62 H350',
  'data-zet': 'M600 42 H690',
  'zet-data': 'M690 62 H600',
  'zet-world': 'M775 80 V150 H600',
  'world-inv': 'M430 160 H405 V70 H350',
  'world-code': 'M430 140 H350',
  'inv-code': 'M245 80 V120',
  'code-inv': 'M295 120 V80',
  'code-critic': 'M265 180 V220',
  'critic-inv': 'M180 255 H160 V35 H180',
  'critic-report': 'M350 250 H690',
  'code-report': 'M350 165 H385 V240 H690', // sample mode: the code checks decide
}

// Live: keep up with the run. A rejection or an accept stays up long enough to read.
const STEP_MS = 900
const CATCH_UP_MS = 250 // when several steps are waiting
const HOLD_MS = { reject: 3000, ok: 1800 }
// Replay: slower, so each step can be followed; the speed menu scales these.
const REPLAY_MS = 2200
const REPLAY_HOLD_MS = { reject: 5500, ok: 3500 }
const SPEEDS = [['Slow', 1], ['Normal', 0.55], ['Fast', 0.3]]

function clip(text, n = 150) {
  const s = String(text ?? '').replace(/\s+/g, ' ').trim()
  return s.length > n ? `${s.slice(0, n - 1)}…` : s
}

function firstIssue(issues) {
  const issue = issues?.[0]
  if (!issue) return ''
  const more = issues.length > 1 ? ` (+${issues.length - 1} more)` : ''
  return `${issue.order_id ? `${issue.order_id}: ` : ''}${clip(issue.problem, 130)}${more}`
}

// One event -> one step: the node working now, the edges the work moved along, and a caption.
function buildSteps(events, sampleMode) {
  const steps = []
  let queried = false // the Data Agent has run a query in its current task
  events.forEach((e, index) => {
    const step = (node, edges, text, tone = 'normal') => steps.push({ node, edges, text, tone, index })
    switch (e.kind) {
      case 'run_started':
        return step('investigator', ['ask'], 'The request goes to the Investigator')
      case 'attempt_started':
        return e.attempt > 1
          ? step('investigator', [], `Attempt ${e.attempt} of ${e.max_attempts}: the Investigator fixes ${e.retry_issues?.length || 0} issue(s) from the critic`)
          : step('investigator', [], `Attempt 1 of ${e.max_attempts}: the Investigator plans which data it needs`)
      case 'data_request':
        queried = false
        return step('data', ['inv-data'], `Investigator asks the Data Agent: “${clip(e.request, 120)}”`)
      case 'query':
        queried = true
        return step('zetaris', ['data-zet'], `Zetaris answers the Data Agent’s SQL: ${e.rows} row(s) in ${e.seconds}s`)
      case 'query_failed':
        return step('data', ['zet-data'], `Query refused: ${clip(e.error, 110)}. The Data Agent fixes its SQL`, 'reject')
      case 'world_model':
        if (e.action === 'record_zetaris_rows') return step('world', ['zet-world'], `World Model records ${clip(e.input, 60)}, each with a stable id and its source`)
        if (e.action === 'provide_retry_context') return step('world', ['world-inv'], 'World Model hands the data already retrieved back to the Investigator')
        if (e.action === 'record_findings') return step('world', [], `World Model records ${clip(e.input, 60)}`)
        return step('world', [], `World Model: ${e.action}`)
      case 'llm_call':
        if (e.agent === 'Data Agent') {
          if (queried) {
            queried = false
            return step('investigator', ['data-inv'], 'The Data Agent hands the rows back to the Investigator, unchanged')
          }
          return step('data', [], `Data Agent wrote its SQL (${e.model || 'model'})`)
        }
        if (e.agent === 'Investigator') return step('investigator', [], `Investigator reasons over the evidence (${e.model || 'model'})`)
        return step('critic', [], `Critic Agent finished its review (${e.model || 'model'})`)
      case 'reminder':
        return step('investigator', [], 'The reply had no findings, so the Investigator is asked once more for just the JSON', 'reject')
      case 'investigator_done':
        return step('code', ['inv-code', 'world-code'], `Investigator submits ${e.findings?.length ?? 'its'} finding(s); code checks verify every evidence item against the World Model`)
      case 'code_checks':
        if (e.passed && sampleMode) return step('code', ['world-code'], 'Code checks passed: every evidence item matches the register', 'ok')
        return e.passed
          ? step('critic', ['code-critic'], 'Code checks passed: every evidence item matches the register. Now the Critic Agent reviews', 'ok')
          : step('investigator', ['code-inv'], `Code checks REJECTED the findings → back to the Investigator. ${firstIssue(e.issues)}`, 'reject')
      case 'critic_started':
        return step('critic', [], 'The Critic Agent judges what code cannot: does each explanation follow from the evidence?')
      case 'verdict':
        if (e.verdict === 'ACCEPT') {
          return e.decided_by === 'code'
            ? step('report', ['code-report'], 'Code checks ACCEPTED (sample mode has no Critic Agent): the report goes to a human', 'ok')
            : step('report', ['critic-report'], 'Critic Agent ACCEPTED: the report goes to a human', 'ok')
        }
        if (e.decided_by === 'code') return undefined // already shown by the code_checks step
        return step('investigator', ['critic-inv'], `Critic Agent sent it back → Investigator. ${firstIssue(e.issues)}`, 'reject')
      case 'run_finished':
        return step('report', [], `Run finished: ${e.status} after ${e.attempts} attempt(s)`, e.status === 'VERIFIED' ? 'ok' : 'normal')
      default:
        return undefined
    }
  })
  return steps
}

// Small live counters under each node, from the events up to the step on screen.
function nodeStats(events) {
  let invCalls = 0, requests = 0, queries = 0, rows = 0, writes = 0, checks = '', critic = '', status = ''
  for (const e of events) {
    if (e.kind === 'llm_call' && e.agent === 'Investigator') invCalls += 1
    if (e.kind === 'data_request') requests += 1
    if (e.kind === 'query') { queries += 1; rows += e.rows || 0 }
    if (e.kind === 'world_model') writes += 1
    if (e.kind === 'code_checks') checks = e.passed ? 'last: passed' : `last: rejected (${e.issues?.length || 0})`
    if (e.kind === 'verdict' && e.decided_by !== 'code') critic = `last: ${e.verdict}`
    if (e.kind === 'run_finished') status = e.status
  }
  return {
    investigator: invCalls && `${invCalls} model call(s)`,
    data: requests && `${requests} request(s)`,
    zetaris: queries && `${queries} quer${queries === 1 ? 'y' : 'ies'} · ${rows} rows`,
    world: writes && `${writes} write(s)`,
    code: checks,
    critic,
    report: status,
  }
}

export default function AgentFlow({ run }) {
  const events = useMemo(() => run.events || [], [run.events])
  const sampleMode = Boolean(run.sample_mode || run.report?.sample_mode)
  const steps = useMemo(() => buildSteps(events, sampleMode), [events, sampleMode])
  // A run opened after it finished starts at its end; a live run plays every step.
  const [shown, setShown] = useState(() => (run.state === 'running' ? 0 : steps.length))
  const [replaying, setReplaying] = useState(false)
  const [speed, setSpeed] = useState(1)

  // Depends on the count and the shown step's tone, not the steps array: polling hands over a new
  // array every tick, and restarting the timer each time would hold a step on screen forever.
  const total = steps.length
  const shownTone = steps[shown - 1]?.tone
  useEffect(() => {
    if (shown >= total) return undefined
    const delay = replaying
      ? (REPLAY_HOLD_MS[shownTone] || REPLAY_MS) * speed
      : HOLD_MS[shownTone] || (total - shown > 4 ? CATCH_UP_MS : STEP_MS)
    const timer = setTimeout(() => setShown((s) => s + 1), delay)
    return () => clearTimeout(timer)
  }, [shown, total, shownTone, replaying, speed])

  const playing = shown < steps.length
  const current = shown > 0 ? steps[shown - 1] : null
  const live = run.state === 'running' || playing
  const visited = new Set(steps.slice(0, shown).map((s) => s.node))
  const stats = nodeStats(current ? events.slice(0, current.index + 1) : [])
  const edgeTone = Object.fromEntries((live && current ? current.edges : []).map((key) => [key, current.tone]))

  let caption = current?.text
  if (!current) caption = run.state === 'running' ? 'Starting…' : 'Finished. Open the report below.'

  return (
    <div className="flow">
      <svg className="flow-svg" viewBox="0 0 880 300" role="img" aria-label="Agent flow: which agent is working now">
        <defs>
          {['normal', 'ok', 'reject'].map((tone) => (
            <marker key={tone} id={`head-${tone}`} viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="12" markerHeight="12" orient="auto-start-reverse">
              <path d="M0 0 L10 5 L0 10 z" className={`flow-head tone-${tone}`} />
            </marker>
          ))}
          <marker id="head-idle" viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="9" markerHeight="9" orient="auto-start-reverse">
            <path d="M0 0 L10 5 L0 10 z" className="flow-head idle" />
          </marker>
        </defs>
        {Object.entries(EDGES).map(([key, d]) => {
          const tone = edgeTone[key]
          if (key === 'code-report' && !tone) return null // only drawn when it is used
          return (
            <path
              key={key}
              d={d}
              className={tone ? `flow-edge active tone-${tone}` : 'flow-edge'}
              markerEnd={`url(#head-${tone || 'idle'})`}
            />
          )
        })}
        {Object.entries(NODES).map(([key, n]) => {
          const active = live && current?.node === key
          const cls = ['flow-node', active && 'active', active && `tone-${current.tone}`, visited.has(key) && 'visited']
            .filter(Boolean)
            .join(' ')
          return (
            <g key={key} className={cls} transform={`translate(${n.x} ${n.y})`}>
              <rect width={n.w || W} height={H} rx="10" />
              <text x="12" y="25" className="flow-title">{n.title}</text>
              <text x="12" y="45" className="flow-sub">{stats[key] || n.sub}</text>
            </g>
          )
        })}
      </svg>
      <div className="flow-caption">
        <p key={shown} className={`flow-text tone-${current?.tone || 'normal'}`} aria-live="polite">
          {live && <span className="flow-live" aria-hidden />}
          {caption}
        </p>
        {run.state !== 'running' && steps.length > 0 && (
          <div className="flow-controls">
            {playing && <span className="muted small">step {shown} / {total}</span>}
            <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))} aria-label="Replay speed">
              {SPEEDS.map(([label, factor]) => (
                <option key={label} value={factor}>{label}</option>
              ))}
            </select>
            {playing ? (
              <button type="button" className="secondary" onClick={() => { setReplaying(false); setShown(total) }}>
                Skip to end
              </button>
            ) : (
              <button type="button" className="secondary" onClick={() => { setReplaying(true); setShown(0) }}>
                ▶ Replay
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
