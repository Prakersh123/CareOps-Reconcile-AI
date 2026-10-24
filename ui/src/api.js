async function call(path, options) {
  const response = await fetch(`/api${path}`, options)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`)
  return body
}

export const api = {
  config: () => call('/config'),
  runs: () => call('/runs'),
  run: (id, since = 0) => call(`/runs/${id}?since=${since}`),
  register: (id) => call(`/runs/${id}/register`),
  start: (request, sampleMode) =>
    call('/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request, sample_mode: sampleMode }),
    }),
}
