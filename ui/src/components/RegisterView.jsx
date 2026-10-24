import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { findingType, shortId } from '../meta.js'

function Attrs({ attrs }) {
  return (
    <span className="attrs">
      {Object.entries(attrs).map(([key, value]) => (
        <span key={key}>
          <span className="muted">{key}</span>=<code>{value === null ? 'null' : String(value)}</code>
        </span>
      ))}
    </span>
  )
}

export default function RegisterView({ runId }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  // App gives this component key={runId}, so each run starts with fresh state.
  useEffect(() => {
    api.register(runId).then(setData).catch((e) => setError(e.message))
  }, [runId])

  if (error) return <div className="banner banner-risk">{error}</div>
  if (!data) return <p className="muted pad">Loading register…</p>

  return (
    <div className="register">
      <p className="muted intro">
        The Meterless World Model engine records everything this run saw and decided: append-only, every write with
        provenance (<code>zetaris</code> data vs <code>derived</code> links vs <code>agent</code> claims), stable ids like{' '}
        <code>invoice:zetaris:I104</code>. Code checks verify evidence against the <code>zetaris</code> writes only.
      </p>

      <section className="card">
        <h2>Entities from Zetaris ({data.entities.length})</h2>
        <table className="grid">
          <thead><tr><th>Id</th><th>Retrieved columns</th></tr></thead>
          <tbody>
            {data.entities.map((e) => (
              <tr key={e.id}><td><code>{e.id}</code></td><td><Attrs attrs={e.attrs} /></td></tr>
            ))}
          </tbody>
        </table>
      </section>

      <div className="two-col">
        <section className="card">
          <h2>Relationships ({data.relationships.length})</h2>
          <ul className="links">
            {data.relationships.map((r) => (
              <li key={r.id}>
                <code>{shortId(r.from)}</code> <span className="rel">—{r.type}→</span> <code>{shortId(r.to)}</code>
                <span className={`badge small tone-${r.source.kind === 'derived' ? 'info' : 'muted'}`}>{r.source.kind}</span>
                {r.valid_to && <span className="muted small"> closed {r.valid_to}</span>}
              </li>
            ))}
          </ul>
        </section>

        <section className="card">
          <h2>Investigator claims ({data.facts.length})</h2>
          <ul className="links">
            {data.facts.map((f) => (
              <li key={f.id} className={f.superseded_by ? 'superseded' : ''}>
                <code>{shortId(f.about)}</code>{' '}
                <span className={`badge small tone-${findingType(f.value?.finding_type).tone}`}>{findingType(f.value?.finding_type).label}</span>{' '}
                <span className="muted small">confidence {f.confidence}{f.superseded_by ? ' · superseded by a later attempt' : ''}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>

      <section className="card">
        <h2>Audit trail ({data.audit.length})</h2>
        <ol className="audit">
          {data.audit.map((a) => (
            <li key={a.seq}>
              <span className="muted small seq">#{a.seq}</span>
              <span className="badge small tone-muted">{a.kind}</span>
              <span className="actor">{a.actor}</span>
              <span className="summary">{a.summary}</span>
            </li>
          ))}
        </ol>
      </section>
    </div>
  )
}
