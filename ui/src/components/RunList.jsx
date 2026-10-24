import { STATUS_TONE, runLabel } from '../meta.js'

export default function RunList({ runs, activeId, onOpen }) {
  return (
    <section className="card run-list">
      <h2>Runs</h2>
      {runs.length === 0 && <p className="muted small">No runs yet.</p>}
      <ul>
        {runs.map((run) => {
          const status = run.state === 'running' ? 'running' : run.state === 'failed' ? 'failed' : run.status
          return (
            <li key={run.run_id}>
              <button className={run.run_id === activeId ? 'run-item active' : 'run-item'} onClick={() => onOpen(run.run_id)}>
                <span className={`dot tone-${STATUS_TONE[status] || 'muted'}`} aria-hidden />
                <span className="run-text">
                  <span className="run-request">{run.request}</span>
                  <span className="muted small">
                    {runLabel(run.run_id)} · {status || '—'}
                    {run.attempts ? ` · ${run.attempts} attempt${run.attempts > 1 ? 's' : ''}` : ''}
                    {run.sample_mode ? ' · sample' : ''}
                  </span>
                </span>
              </button>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
