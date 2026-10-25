import { STATUS_TONE } from '../meta.js'
import AgentFlow from './AgentFlow.jsx'

export default function Pipeline({ run, maxAttempts }) {
  const attempts = run.events.filter((e) => e.kind === 'attempt_started').length || run.report?.attempts || 0
  const status = run.state === 'running' ? 'running' : run.state === 'failed' ? 'failed' : run.report?.status

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
      <AgentFlow key={run.run_id} run={run} />
    </section>
  )
}
