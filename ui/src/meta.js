// How each finding type is shown: label and tone (ok | warn | risk | info | muted).
export const FINDING_TYPES = {
  RECONCILED: { label: 'Reconciled', tone: 'ok' },
  MISSING_INVOICE_CANDIDATE: { label: 'Missing invoice (candidate)', tone: 'warn' },
  UNPAID_INVOICE: { label: 'Unpaid invoice', tone: 'warn' },
  UNPAID_REPLACEMENT_INVOICE: { label: 'Unpaid replacement invoice', tone: 'info' },
  CANCELLED_WITHOUT_REPLACEMENT: { label: 'Cancelled, no replacement', tone: 'risk' },
  POSSIBLE_DUPLICATE_BILLING: { label: 'Possible duplicate billing', tone: 'risk' },
  INSUFFICIENT_EVIDENCE: { label: 'Insufficient evidence', tone: 'muted' },
}

export function findingType(type) {
  return FINDING_TYPES[type] || { label: type || 'Unknown', tone: 'muted' }
}

export const STATUS_TONE = { VERIFIED: 'ok', INCONCLUSIVE: 'warn', failed: 'risk', running: 'info' }

export function shortId(id) {
  return String(id ?? '').split(':').pop()
}

export function formatTime(iso) {
  if (!iso) return ''
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
}

export function runLabel(runId) {
  // run-20261024-120000 -> 24 Oct, 12:00:00
  const m = /^run-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/.exec(runId || '')
  if (!m) return runId
  const date = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6])
  return date.toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}
