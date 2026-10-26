import { STATUS_TONE, findingType, formatTime } from '../meta.js'

function show(value) {
  return value === null || value === undefined ? 'null' : String(value)
}

function FindingCard({ finding }) {
  const type = findingType(finding.finding_type)
  const verified = finding.evidence.filter((e) => e.verified).length
  return (
    <article className={`card finding tone-border-${type.tone}`}>
      <header className="finding-head">
        <h3>{finding.order_id}</h3>
        <span className={`badge tone-${type.tone}`}>{type.label}</span>
      </header>
      <div className="finding-meta">
        <span>confidence <b>{finding.confidence}</b></span>
        <span className={finding.requires_human_review ? 'review yes' : 'review'}>
          {finding.requires_human_review ? 'Needs human review' : 'No review needed'}
        </span>
        {finding.invoice_ids?.length > 0 && <span>invoices <b>{finding.invoice_ids.join(', ')}</b></span>}
      </div>
      <p>{finding.explanation}</p>
      <dl className="finding-notes">
        <dt>Uncertainty</dt>
        <dd>{finding.uncertainty}</dd>
        <dt>Recommended action</dt>
        <dd>{finding.recommended_action}</dd>
      </dl>
      <details open={finding.evidence.length <= 4}>
        <summary>
          Evidence · {verified}/{finding.evidence.length} verified against Zetaris
        </summary>
        <table className="evidence">
          <thead>
            <tr><th>Record</th><th>Field</th><th>Value</th><th aria-label="Verified" /></tr>
          </thead>
          <tbody>
            {finding.evidence.map((item, i) => (
              <tr key={i} title={item.detail}>
                <td>{item.invoice_id ?? `${finding.order_id} (order row)`}</td>
                <td><code>{item.field}</code></td>
                <td><code>{show(item.value)}</code></td>
                <td className={item.verified ? 'ok-text' : 'risk-text'}>{item.verified ? '✓' : `✗ ${item.check}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </article>
  )
}

function CriticHistory({ history }) {
  return (
    <section className="card">
      <h2>What the critic caught</h2>
      <ol className="attempts">
        {history.map((attempt) => (
          <li key={attempt.number}>
            <div className="attempt-head">
              <b>Attempt {attempt.number}</b>
              <span className={`badge tone-${attempt.verdict === 'ACCEPT' ? 'ok' : 'warn'}`}>{attempt.verdict ?? '—'}</span>
              {attempt.decided_by && (
                <span className="muted small">by {attempt.decided_by === 'code' ? 'code checks (LLM skipped)' : 'LLM critic'}</span>
              )}
            </div>
            {attempt.issues.length > 0 ? (
              <ul className="issues">
                {attempt.issues.map((issue, i) => (
                  <li key={i}>
                    <code className={String(issue.check).startsWith('code:') ? 'tag tag-code' : 'tag tag-llm'}>{issue.check}</code>{' '}
                    {issue.order_id && <b>{issue.order_id}: </b>}
                    {issue.problem}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted small">No issues.</p>
            )}
          </li>
        ))}
      </ol>
    </section>
  )
}

export default function Report({ report }) {
  const { summary } = report
  const tone = STATUS_TONE[report.status] || 'muted'
  if (report.status === 'REFUSED') {
    // A request to change data: the read-only refusal is the whole answer.
    return (
      <div className="report">
        <section className={`card status-banner tone-border-${tone}`}>
          <div>
            <span className={`badge big tone-${tone}`}>{report.status}</span>
            <span className="muted">{formatTime(report.generated_at)}</span>
          </div>
          <p>{report.message}</p>
          <p className="muted small">No agent was started, and no data was read or changed.</p>
        </section>
      </div>
    )
  }
  return (
    <div className="report">
      <section className={`card status-banner tone-border-${tone}`}>
        <div>
          <span className={`badge big tone-${tone}`}>{report.status}</span>
          <span className="muted">
            after {report.attempts} attempt{report.attempts > 1 ? 's' : ''} · {formatTime(report.generated_at)}
          </span>
        </div>
        {report.sample_mode && (
          <p className="banner banner-info small">
            Sample mode: bundled synthetic rows instead of Zetaris and rule-based stand-ins instead of the LLM agents.
            The World Model register and the code checks ran for real; the LLM critic did not run.
          </p>
        )}
        <p className="muted small">
          {report.status === 'VERIFIED'
            ? report.sample_mode
              ? 'Code checks accepted these findings.'
              : 'Code checks and the LLM critic accepted these findings.'
            : 'The critic did not accept the findings within the attempt limit — treat them as unverified.'}
        </p>
      </section>

      <div className="tiles">
        <div className="tile"><span className="tile-value">{summary.findings}</span><span className="muted small">orders with findings</span></div>
        <div className="tile"><span className="tile-value">{summary.needs_human_review}</span><span className="muted small">need human review</span></div>
        <div className="tile">
          <span className="tile-value">{summary.evidence_verified}/{summary.evidence_items}</span>
          <span className="muted small">evidence items verified against Zetaris</span>
        </div>
        <div className="tile">
          <span className="tile-value">{report.register.events}</span>
          <span className="muted small">
            register events · {report.register.orders_retrieved} orders, {report.register.invoices_retrieved} invoices
          </span>
        </div>
      </div>

      {report.findings_unparsed && (
        <section className="card banner-risk"><b>Findings were not valid JSON:</b><pre>{report.findings_unparsed}</pre></section>
      )}
      <div className="findings">
        {report.findings.map((finding) => <FindingCard key={finding.order_id} finding={finding} />)}
      </div>

      <CriticHistory history={report.attempt_history} />
    </div>
  )
}
