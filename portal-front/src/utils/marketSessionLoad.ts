interface SessionLogFirstOptions<TLog, TDocument> {
  loadLog: () => Promise<TLog>
  loadDocuments: () => Promise<TDocument[]>
  shouldLoadDocuments?: (log: TLog) => boolean
  publish: (log: TLog, documents: TDocument[]) => void
}

interface SingleFlightOptions {
  failureCooldownMs?: number
  now?: () => number
}

export function createSingleFlight<TKey, TValue>(
  load: (key: TKey) => Promise<TValue>,
  { failureCooldownMs = 0, now = Date.now }: SingleFlightOptions = {},
): (key: TKey) => Promise<TValue> {
  const requests = new Map<TKey, { promise: Promise<TValue>; failedUntil?: number }>()

  return (key: TKey) => {
    const current = requests.get(key)
    if (current) {
      if (current.failedUntil === undefined || now() < current.failedUntil) return current.promise
      requests.delete(key)
    }

    const started = load(key)
    const entry: { promise: Promise<TValue>; failedUntil?: number } = { promise: started }
    requests.set(key, entry)
    void started.then(
      () => {
        if (requests.get(key) === entry) requests.delete(key)
      },
      () => {
        if (requests.get(key) !== entry) return
        if (failureCooldownMs > 0) entry.failedUntil = now() + failureCooldownMs
        else requests.delete(key)
      },
    )
    return started
  }
}

interface SessionDocumentReferenceItem {
  data?: {
    temp_documents?: readonly unknown[]
    sourceDocuments?: readonly unknown[]
  }
}

export function sessionHasDocumentReferences(items: readonly SessionDocumentReferenceItem[]): boolean {
  return items.some(item =>
    (item.data?.temp_documents?.length ?? 0) > 0
    || (item.data?.sourceDocuments?.length ?? 0) > 0
  )
}

export async function loadSessionLogFirst<TLog, TDocument>({
  loadLog,
  loadDocuments,
  shouldLoadDocuments = () => true,
  publish,
}: SessionLogFirstOptions<TLog, TDocument>): Promise<TLog> {
  const log = await loadLog()

  publish(log, [])
  if (!shouldLoadDocuments(log)) return log

  const documents = loadDocuments().catch(() => null)
  void documents.then(value => {
    if (value !== null) publish(log, value)
  })

  return log
}
