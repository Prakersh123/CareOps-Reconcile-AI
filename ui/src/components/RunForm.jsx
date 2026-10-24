import { useEffect, useState } from 'react'

export default function RunForm({ config, disabled, onStart }) {
  const [request, setRequest] = useState('')
  const [sample, setSample] = useState(null) // null = follow the server's SAMPLE_MODE default

  useEffect(() => {
    if (config && !request) setRequest(config.default_request)
    // only fill the box the first time the config arrives
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config])

  const submit = (event) => {
    event.preventDefault()
    if (!disabled) onStart(request, sample ?? config?.sample_mode_default ?? false)
  }

  return (
    <form className="card run-form" onSubmit={submit}>
      <h2>New investigation</h2>
      <label htmlFor="request" className="muted small">
        Ask in plain language. Agents read Zetaris; they never change billing records.
      </label>
      <textarea
        id="request"
        rows={5}
        value={request}
        onChange={(e) => setRequest(e.target.value)}
        placeholder="e.g. Check order L103: duplicate billing or a valid replacement?"
      />
      {config?.examples?.length > 0 && (
        <div className="examples">
          {config.examples.map((example) => (
            <button type="button" key={example} className="example" onClick={() => setRequest(example)} title={example}>
              {example.length > 58 ? `${example.slice(0, 56)}…` : example}
            </button>
          ))}
        </div>
      )}
      <label className="toggle small" title="Bundled sample rows and rule-based stand-ins: no Zetaris, model or network. The World Model register and code checks still run for real.">
        <input
          type="checkbox"
          checked={sample ?? config?.sample_mode_default ?? false}
          onChange={(e) => setSample(e.target.checked)}
        />
        Sample mode (offline, no Zetaris or LLM)
      </label>
      <button type="submit" className="primary" disabled={disabled || !request.trim()}>
        {disabled ? 'An investigation is running…' : 'Run investigation'}
      </button>
    </form>
  )
}
