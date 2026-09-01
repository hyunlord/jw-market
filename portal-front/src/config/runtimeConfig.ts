export interface RuntimeConfig {
  apiBaseUrl: string
  googleClientId: string
  genosNavigationUrl: string
  routerBasename: string
  marketDocumentWorkflowId: number
  marketAcceptedUploadEnabled: boolean
}

let activeConfig: RuntimeConfig | null = null

function requiredString(value: unknown, key: keyof RuntimeConfig): string {
  if (typeof value !== 'string' || value.length === 0 || value.trim() !== value) {
    throw new Error(`Invalid runtime config: ${key} must be a non-empty string`)
  }
  return value
}

function requireHttpUrl(value: string, key: keyof RuntimeConfig): void {
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    throw new Error(`Invalid runtime config: ${key} must be an absolute URL`)
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error(`Invalid runtime config: ${key} must use http or https`)
  }
}

export function validateRuntimeConfig(value: unknown): RuntimeConfig {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('Invalid runtime config: expected an object')
  }
  const input = value as Record<string, unknown>
  const apiBaseUrl = requiredString(input.apiBaseUrl, 'apiBaseUrl')
  const googleClientId = requiredString(input.googleClientId, 'googleClientId')
  const genosNavigationUrl = requiredString(input.genosNavigationUrl, 'genosNavigationUrl')
  const routerBasename = requiredString(input.routerBasename, 'routerBasename')
  const marketDocumentWorkflowId = input.marketDocumentWorkflowId
  const marketAcceptedUploadEnabled = input.marketAcceptedUploadEnabled

  if (!apiBaseUrl.startsWith('/')) requireHttpUrl(apiBaseUrl, 'apiBaseUrl')
  if (!googleClientId.endsWith('.apps.googleusercontent.com')) {
    throw new Error('Invalid runtime config: googleClientId has an unexpected format')
  }
  requireHttpUrl(genosNavigationUrl, 'genosNavigationUrl')
  if (routerBasename !== '/') {
    throw new Error('Invalid runtime config: routerBasename must be /')
  }
  if (!Number.isSafeInteger(marketDocumentWorkflowId) || (marketDocumentWorkflowId as number) <= 0) {
    throw new Error('Invalid runtime config: marketDocumentWorkflowId must be a positive integer')
  }
  if (typeof marketAcceptedUploadEnabled !== 'boolean') {
    throw new Error('Invalid runtime config: marketAcceptedUploadEnabled must be a boolean')
  }

  return {
    apiBaseUrl,
    googleClientId,
    genosNavigationUrl,
    routerBasename,
    marketDocumentWorkflowId: marketDocumentWorkflowId as number,
    marketAcceptedUploadEnabled,
  }
}

export function installRuntimeConfig(value: unknown): RuntimeConfig {
  activeConfig = validateRuntimeConfig(value)
  return activeConfig
}

export async function loadRuntimeConfig(
  fetchConfig: typeof fetch = fetch,
): Promise<RuntimeConfig> {
  const response = await fetchConfig('/config.json', { cache: 'no-store' })
  if (!response.ok) {
    throw new Error(`Runtime config load failed with HTTP ${response.status}`)
  }
  return installRuntimeConfig(await response.json())
}

export function getRuntimeConfig(): RuntimeConfig {
  if (!activeConfig) throw new Error('Runtime config has not been loaded')
  return activeConfig
}

export function resolveApiUrl(path: string): string {
  if (!path.startsWith('/')) return path
  const { apiBaseUrl } = getRuntimeConfig()
  if (apiBaseUrl === '/') return path
  return `${apiBaseUrl.replace(/\/$/, '')}${path}`
}

export function resolvePortalPath(path: string): string {
  if (!path.startsWith('/')) throw new Error('Portal path must start with /')
  getRuntimeConfig()
  return path
}
