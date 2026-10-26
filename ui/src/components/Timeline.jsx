import { findingType } from '../meta.js'

function time(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : ''
}

function Issues({ issues }) {
  return (
    <ul className="issues">
      {issues.map((issue, i) => (
        <li key={i}>
          <code className={String(issue.check).startsWith('code:') ? 'tag tag-code' : 'tag tag-llm'}>{issue.check}</code>{' '}
          {issue.order_id && <b>{issue.order_id}: </b>}
          {issue.problem}
        </li>
      ))}
    </ul>
  )
}

function Event({ event }) {
  switch (event.kind) {
    case 'run_started':
      return <><b>Investigation started</b><p className="muted">{event.request}</p></>
    case 'attempt_started':
      return (
        <>
          <b>Attempt {event.attempt} of {event.max_attempts}</b>
          {event.retry_issues?.length > 0 && (
            <>
              <p className="muted">
                Retrying with {event.retry_issues.length} critic issue(s) and the data already in the World Model register:
              </p>
              <Issues issues={event.retry_issues} />
            </>
          )}
        </>
      )
    case 'data_request':
      return <><b>Investigator → Data Agent</b><p>“{event.request}”</p></>
    case 'query':
      return (
        <>
          <b>Zetaris query</b>{' '}
          <span className="muted small">
            {event.rows} row(s){event.truncated ? ` of ${event.total} (truncated)` : ''} · {event.seconds}s
          </span>
          <pre className="sql">{event.sql}</pre>
        </>
      )
    case 'query_failed':
      return <><b>Query failed</b> <span className="muted small">(the agent sees the error and can fix its SQL)</span><pre className="sql">{event.sql}</pre><p className="risk-text">{event.error}</p></>
    case 'llm_call':
      return (
        <>
          <b>{event.agent} · model call</b>{' '}
          <code>{event.model || 'unknown model'}</code> <span className="muted small">{event.completion}</span>
        </>
      )
    case 'world_model':
      return (
        <>
          <b>World Model (Meterless engine)</b> <code>{event.action}</code>
          <p className="muted">{event.input} → {event.output}</p>
        </>
      )
    case 'reminder':
      return <><b>Empty reply ({event.completion})</b><p className="muted">Asked the investigator once more for the findings JSON, in the same conversation.</p></>
    case 'investigator_done':
      return (
        <>
          <b>Investigator finished</b>{' '}
          <span className="muted small">{event.data_requests} data request(s) · {event.completion}</span>
          {event.findings ? (
            <div className="finding-chips">
              {event.findings.map((f, i) => (
                <span key={i} className={`badge tone-${findingType(f.finding_type).tone}`}>
                  {f.order_id}: {findingType(f.finding_type).label} · {f.confidence}
                </span>
              ))}
            </div>
          ) : (
            <p className="risk-text">No valid findings JSON.</p>
          )}
        </>
      )
    case 'code_checks':
      return event.passed ? (
        <><b>Code checks passed</b><p className="muted">Every evidence item matches what Zetaris returned (World Model register).</p></>
      ) : (
        <><b>Code checks found {event.issues.length} issue(s)</b> <span className="muted small">— LLM critic skipped</span><Issues issues={event.issues} /></>
      )
    case 'critic_started':
      return <><b>LLM critic reviewing</b><p className="muted">Logic, candidate vs confirmed, replacement vs duplicate, coverage.</p></>
    case 'verdict':
      return (
        <>
          <b>Verdict: {event.verdict}</b>{' '}
          <span className="muted small">decided by {event.decided_by === 'code' ? 'code checks' : 'LLM critic'}</span>
          {event.issues?.length > 0 && <Issues issues={event.issues} />}
        </>
      )
    case 'critic_failed':
      return (
        <>
          <b>Critic Agent did not answer</b>
          <p className="risk-text">{event.error}</p>
          <p className="muted small">The findings passed the code checks; the run ends INCONCLUSIVE instead of failing.</p>
        </>
      )
    case 'write_refused':
      return (
        <>
          <b>Refused: this system is read-only</b>
          <p className="muted small">The request asks to change data (“{event.matched}”).</p>
          <p className="risk-text">{event.message}</p>
        </>
      )
    case 'run_finished':
      return <><b>Finished: {event.status}</b> <span className="muted small">after {event.attempts} attempt(s)</span></>
    default:
      return <b>{event.kind}</b>
  }
}

const TONE = {
  query_failed: 'risk', code_checks: (e) => (e.passed ? 'ok' : 'warn'), verdict: (e) => (e.verdict === 'ACCEPT' ? 'ok' : 'warn'),
  run_finished: (e) => (e.status === 'VERIFIED' ? 'ok' : 'warn'), attempt_started: 'info', reminder: 'warn',
  llm_call: (e) => (e.finish === 'length' ? 'warn' : 'muted'), world_model: 'info',
}

export default function Timeline({ events, running }) {
  if (events.length === 0) {
    return <p className="muted pad">{running ? 'Starting…' : 'No live timeline for runs from earlier server sessions; see the report.'}</p>
  }
  return (
    <ol className="timeline card">
      {events.map((event, i) => {
        const tone = typeof TONE[event.kind] === 'function' ? TONE[event.kind](event) : TONE[event.kind] || 'muted'
        return (
          <li key={i} className={`tl-item tone-border-${tone} ${event.kind === 'attempt_started' ? 'tl-attempt' : ''}`}>
            <span className="tl-time">{time(event.at)}</span>
            <div className="tl-body"><Event event={event} /></div>
          </li>
        )
      })}
      {running && <li className="tl-item tl-pending"><span className="tl-time" /><div className="tl-body muted">working…</div></li>}
    </ol>
  )
}
