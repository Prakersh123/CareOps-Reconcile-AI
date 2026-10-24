import { STATUS_TONE } from '../meta.js'

// Which stage the run is in, read from its latest progress events.
function stageOf(run) {
  if (run.state !== 'running') return 'done'
  const last = [...run.events].reverse().find((e) =>
    ['attempt_started', 'investigator_done', 'code_checks', 'critic_started', 'verdict'].includes(e.kind),
  )
  if (!last || last.kind === 'attempt_started') return 'investigator'
  if (last.kind === 'investigator_done') return 'code'
  if (last.kind === 'critic_started') return 'critic'
  if (last.kind === 'code_checks') return last.passed ? 'critic' : 'investigator'
  return 'investigator' // a verdict while still running means a retry is starting
}

const STAGES = [
  ['investigator', 'Investigator + Data Agent', 'queries Zetaris USL'],
  ['code', 'Code checks', 'evidence vs World Model'],
  ['critic', 'LLM critic', 'judgment'],
  ['done', 'Result', 'for human review'],
]

export default function Pipeline({ run, maxAttempts }) {
  const stage = stageOf(run)
  const attempts = run.events.filter((e) => e.kind === 'attempt_started').length || run.report?.attempts || 0
  const status = run.state === 'running' ? 'running' : run.state === 'failed' ? 'failed' : run.report?.status
  const current = STAGES.findIndex(([key]) => key === stage)

  return (
    <section className="card pipeline">
      <div className="pipeline-head">
        <div>
          <span className="muted small">{run.run_id}</span>
          <p className="request">{run.request}</p>
        </div>
        <div className="pipeline-status">
          <span className={`badge tone-${STATUS_TONE[status] || 'muted'}`}>{status === 'running' ? 'Running' : status}</span>
          {(run.sample_mode || run.report?.sample_mode) && <span className="badge tone-info">sample mode</span>}
          {attempts > 0 && (
            <span className="muted small">
              attempt {attempts} / {maxAttempts}
            </span>
          )}
        </div>
      </div>
      <ol className="stages">
        {STAGES.map(([key, label, hint], index) => {
          const state = run.state !== 'running' ? 'complete' : index < current ? 'complete' : index === current ? 'current' : 'todo'
          return (
            <li key={key} className={`stage ${state}`}>
              <span className="stage-dot">{state === 'complete' ? '✓' : index + 1}</span>
              <span>
                <b>{label}</b>
                <span className="muted small">{hint}</span>
              </span>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
