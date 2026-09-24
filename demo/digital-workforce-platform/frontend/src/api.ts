export type Worker = { worker_id: string; name: string; description: string; runtime_name: string; enabled: boolean; editable: boolean; capabilities: string[]; mcp_server_names: string[]; mcp_tools: string[]; skill_catalog: { name: string; description: string; origin: string }[] }
export type Template = { id: string; name: string; mode: string; worker_ids: string[] }
export type Orchestration = { orchestration_id: string; session_id: string; goal: string; mode: string; status: string; continued_from?: string; result?: { summary: string; worker_results: { task_id: string; worker_id: string; status: string; output: { text?: string } }[] }; session?: { messages: string[] }; effective: { worker_ids: string[]; facts?: Record<string, unknown>; react?: Record<string, unknown> } }
export type WorkflowEvent = { seq: number; ts: string; type: string; payload: { task_id?: string; worker_id?: string; status?: string; instruction?: string; error?: string } }
export type Skill = { name: string; source: string; content: string; description: string }
export type SkillPreview = { name: string; description: string; body: string; digest: string; allowed_tools: string[] }
export type McpServer = { name: string; transport: string; url: string; command: string; args: string[]; allowed_tools: string[]; blocked_tools: string[] }
export type AuthSession = { access_token: string; refresh_token: string; token_type: string; expires_in: number; tenant_id: string }
export type CurrentUser = { user_id: string; tenant_id: string; roles: string[]; memberships: { tenant_id: string; role: string }[] }

const authStorageKey = 'digital-workforce.auth'

function storedSession(): AuthSession | undefined {
  const value = sessionStorage.getItem(authStorageKey)
  return value ? JSON.parse(value) as AuthSession : undefined
}

function storeSession(session: AuthSession): AuthSession {
  sessionStorage.setItem(authStorageKey, JSON.stringify(session))
  return session
}

function authHeaders(): Record<string, string> {
  const session = storedSession()
  return session ? { Authorization: `Bearer ${session.access_token}`, ...(session.tenant_id ? { 'X-Tenant': session.tenant_id } : {}) } : {}
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, { headers: { 'Content-Type': 'application/json', ...authHeaders(), ...(options?.headers ?? {}) }, ...options })
  if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail ?? `请求失败 (${response.status})`)
  if (response.status === 204 || response.headers.get('Content-Length') === '0') return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  session: () => storedSession(),
  setTenant: (tenantId: string) => {
    const session = storedSession()
    if (!session) throw new Error('请先登录')
    return storeSession({ ...session, tenant_id: tenantId })
  },
  register: async (body: { username: string; password: string; display_name: string; tenant_id: string; tenant_name: string }) => {
    const response = await request<AuthSession & { user: { user_id: string; username: string; display_name: string } }>('/auth/register', { method: 'POST', body: JSON.stringify(body) })
    storeSession(response)
    return response
  },
  login: async (body: { username: string; password: string; tenant_id?: string }) => {
    const response = await request<AuthSession>('/auth/login', { method: 'POST', body: JSON.stringify(body) })
    return storeSession(response)
  },
  currentUser: () => request<CurrentUser>('/auth/me'),
  logout: async () => {
    try { await request<void>('/auth/logout', { method: 'POST' }) } finally { sessionStorage.removeItem(authStorageKey) }
  },
  workers: () => request<Worker[]>('/workers'),
  worker: (workerId: string) => request<Worker>(`/workers/${workerId}`),
  templates: () => request<Template[]>('/orchestrations/templates'),
  listRuns: () => request<Orchestration[]>('/orchestrations'),
  run: (id: string) => request<Orchestration>(`/orchestrations/${id}`),
  create: (body: unknown) => request<{ orchestration_id: string; session_id: string; status: string }>('/orchestrations', { method: 'POST', body: JSON.stringify(body) }),
  continueRun: (orchestrationId: string, body: unknown) => request<{ orchestration_id: string; session_id: string; status: string }>(`/orchestrations/${orchestrationId}/continue`, { method: 'POST', body: JSON.stringify(body) }),
  approve: (orchestrationId: string, taskId: string, decision: 'approve' | 'reject', note: string) => request<{ status: string }>(`/orchestrations/${orchestrationId}/approvals/${taskId}`, { method: 'POST', body: JSON.stringify({ decision, note }) }),
  skills: () => request<Skill[]>('/skills'),
  createSkill: (body: Pick<Skill, 'name' | 'content' | 'source'>) => request<Skill>('/skills', { method: 'POST', body: JSON.stringify(body) }),
  previewSkill: (name: string) => request<SkillPreview>(`/skills/${name}/preview`, { method: 'POST' }),
  deleteSkill: (name: string) => request<void>(`/skills/${name}`, { method: 'DELETE' }),
  mcpServers: () => request<McpServer[]>('/mcp-servers'),
  createMcpServer: (body: Omit<McpServer, 'args' | 'allowed_tools' | 'blocked_tools'> & Partial<Pick<McpServer, 'args' | 'allowed_tools' | 'blocked_tools'>>) => request<McpServer>('/mcp-servers', { method: 'POST', body: JSON.stringify(body) }),
  updateMcpServer: (name: string, body: McpServer) => request<McpServer>(`/mcp-servers/${name}`, { method: 'PUT', body: JSON.stringify(body) }),
  deleteMcpServer: (name: string) => request<void>(`/mcp-servers/${name}`, { method: 'DELETE' }),
  probeMcpServer: (name: string) => request<{ ok: boolean; message: string; tools: string[] }>(`/mcp-servers/${name}/probe`, { method: 'POST' }),
  createWorker: (body: { worker_id: string; name: string; description: string; runtime_name: string; capabilities: string[]; mcp_server_names: string[] }) => request<Worker>('/workers', { method: 'POST', body: JSON.stringify(body) }),
  updateWorker: (workerId: string, body: { worker_id: string; name: string; description: string; runtime_name: string; capabilities: string[]; mcp_server_names: string[] }) => request<Worker>(`/workers/${workerId}`, { method: 'PUT', body: JSON.stringify(body) }),
  deleteWorker: (workerId: string) => request<void>(`/workers/${workerId}`, { method: 'DELETE' }),
  probeWorker: (workerId: string) => request<{ ok: boolean; message: string }>(`/workers/${workerId}/probe`, { method: 'POST' }),
}
