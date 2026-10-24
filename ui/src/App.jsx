import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import RunForm from './components/RunForm.jsx'
import RunList from './components/RunList.jsx'
import Pipeline from './components/Pipeline.jsx'
import Timeline from './components/Timeline.jsx'
import Report from './components/Report.jsx'
import RegisterView from './components/RegisterView.jsx'

const POLL_MS = 1500

export default function App() {
  const [config, setConfig] = useState(null)
  const [runs, setRuns] = useState([])
  const [run, setRun] = useState(null) // {run_id, request, state, started_at, events, report, error}
  const [tab, setTab] = useState('report')
  const [error, setError] = useState(null)
  const pollRef = useRef(null)

  const refreshRuns = useCallback(() => api.runs().then(setRuns).catch(() => {}), [])

  const stopPolling = () => {
    clearTimeout(pollRef.current)
    pollRef.current = null
  }

  const follow = useCallback(
    (runId, justStarted = false) => {
      stopPolling()
      let next = 0
      let watchedRunning = justStarted // started here, or seen in progress: switch to its report when done
      const tick = async () => {
        try {
          const body = await api.run(runId, next)
          next = body.next
          setRun((current) => ({
            ...body,
            events: current && current.run_id === runId ? [...current.events, ...body.events] : body.events,
          }))
          if (body.state === 'running') {
            watchedRunning = true
            pollRef.current = setTimeout(tick, POLL_MS)
          } else {
            // A run we watched just finished: show its result. Opening an old run keeps the chosen tab.
            if (watchedRunning && body.report) setTab('report')
            refreshRuns()
          }
        } catch (e) {
          setError(e.message)
        }
      }
      tick()
    },
    [refreshRuns],
  )

  useEffect(() => {
    api.config().then(setConfig).catch((e) => setError(`API not reachable: ${e.message}`))
    api.runs().then((list) => {
      setRuns(list)
      const running = list.find((r) => r.state === 'running')
      // #run-20261024-120000 opens that run; #run-20261024-120000/register opens it on that tab
      const [linked, linkedTab] = window.location.hash.slice(1).split('/')
      if (running) {
        setTab('timeline') // a running run has no report yet; show it happening
        follow(running.run_id)
      } else if (list.some((r) => r.run_id === linked)) {
        if (['report', 'timeline', 'register'].includes(linkedTab)) setTab(linkedTab)
        follow(linked)
      }
    })
    return stopPolling
  }, [follow])

  const start = async (request, sampleMode) => {
    setError(null)
    try {
      const started = await api.start(request, sampleMode)
      setRun({ run_id: started.run_id, request: started.request, state: 'running', events: [], report: null })
      setTab('timeline')
      refreshRuns()
      follow(started.run_id, true)
    } catch (e) {
      // Someone (another tab or browser) is already running one: show that run instead of just an error.
      const busy = /Run (\S+) is still in progress/.exec(e.message)
      if (busy) {
        setTab('timeline')
        refreshRuns()
        follow(busy[1], true)
      } else {
        setError(e.message)
      }
    }
  }

  const open = (runId) => {
    setError(null)
    setRun(null)
    setTab(runs.find((r) => r.run_id === runId)?.state === 'running' ? 'timeline' : 'report')
    follow(runId)
  }

  // Keep the URL pointing at what is on screen, so a run (and tab) can be linked to.
  useEffect(() => {
    if (run?.run_id) window.history.replaceState(null, '', `#${run.run_id}/${tab}`)
  }, [run?.run_id, tab])

  const running = run?.state === 'running'
  const busy = runs.some((r) => r.state === 'running')

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden>✚</span>
          <div>
            <h1>CareOps Reconcile AI</h1>
            <p>Lab orders × billing reconciliation across fragmented sources</p>
          </div>
        </div>
        <div className="stack-chips">
          <span className="chip">Zetaris USL</span>
          <span className="chip">LangGraph agents</span>
          <span className="chip">Meterless World Model</span>
          {config && <span className="chip chip-model" title="CAREOPS_MODEL">{config.model}</span>}
        </div>
      </header>

      <div className="layout">
        <aside className="sidebar">
          <RunForm config={config} disabled={busy} onStart={start} />
          <RunList runs={runs} activeId={run?.run_id} onOpen={open} />
        </aside>

        <main className="main">
          {error && <div className="banner banner-risk">{error}</div>}
          {config?.data && !config.data.ready && (
            <DataBanner data={config.data} onRecheck={() => api.config().then(setConfig).catch(() => {})} />
          )}
          {!run && <Welcome />}
          {run && (
            <>
              <Pipeline run={run} maxAttempts={config?.max_attempts ?? 3} />
              {run.state === 'failed' && <div className="banner banner-risk">Run failed: {run.error}</div>}
              <nav className="tabs" role="tablist">
                {[
                  ['report', 'Report', !run.report],
                  ['timeline', `Timeline${running ? ' · live' : ''}`, false],
                  ['register', 'World Model register', running || !run.report],
                ].map(([key, label, disabled]) => (
                  <button
                    key={key}
                    role="tab"
                    aria-selected={tab === key}
                    className={tab === key ? 'tab active' : 'tab'}
                    disabled={disabled}
                    onClick={() => setTab(key)}
                  >
                    {label}
                  </button>
                ))}
              </nav>
              {tab === 'report' && run.report && <Report report={run.report} />}
              {tab === 'report' && !run.report && <p className="muted pad">The report appears when the run finishes.</p>}
              {tab === 'timeline' && <Timeline events={run.events} running={running} />}
              {tab === 'register' && run.report && <RegisterView key={run.run_id} runId={run.run_id} />}
            </>
          )}
        </main>
      </div>
    </div>
  )
}

// Shown when the real pipeline cannot run: Zetaris unreachable, or the CareOps data not set up yet.
function DataBanner({ data, onRecheck }) {
  return (
    <div className="banner banner-warn data-banner">
      <div>
        <b>{data.problem === 'data' ? 'Zetaris data is not set up yet.' : 'Zetaris is not reachable.'}</b>{' '}
        {data.detail}
        {data.problem === 'data' && (
          <>
            {' '}Run <code>python scripts/setup_zetaris.py</code> once, or switch on <b>Sample mode</b> to try it offline.
          </>
        )}
      </div>
      <button type="button" className="secondary" onClick={onRecheck}>
        Check again
      </button>
    </div>
  )
}

function Welcome() {
  return (
    <section className="welcome card">
      <h2>How an investigation runs</h2>
      <ol className="how">
        <li>
          <b>Investigator</b> plans the data it needs and asks the <b>Data Agent</b>, which writes read-only SQL over the
          Zetaris Unified Semantic Layer (lab orders + billing Postgres + billing history, joined in one logical table).
        </li>
        <li>
          Every row Zetaris returns is recorded in a <b>World Model register</b> (Meterless engine): stable ids, provenance,
          an append-only audit log.
        </li>
        <li>
          <b>Code checks</b> verify each evidence item against the register. Anything wrong goes straight back to the
          investigator; only clean findings reach the <b>LLM critic</b>.
        </li>
        <li>
          The critic accepts or sends it back (max 3 attempts). The result is a report for <b>human review</b> — the
          system never changes billing records.
        </li>
      </ol>
      <p className="muted">Start with a question on the left, or open a previous run.</p>
    </section>
  )
}
