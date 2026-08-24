import type {
  ChatSummary,
  CodeContextPayload,
  ReviewAction,
  ReviewDepth,
  ReviewTargetType,
  ReviewerProfile,
  SessionMessagesPayload,
  WebuiThreadPersistedPayload,
} from "./types";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

export interface ApiAuth {
  token: string;
  refreshAuth: () => Promise<string | null>;
}

async function fetchWithToken(
  url: string,
  token: string,
  init?: RequestInit,
): Promise<Response> {
  return fetch(url, {
    ...(init ?? {}),
    headers: {
      ...(init?.headers ?? {}),
      Authorization: `Bearer ${token}`,
    },
    credentials: "same-origin",
  });
}

async function request<T>(
  url: string,
  auth: ApiAuth,
  init?: RequestInit,
): Promise<T> {
  let res = await fetchWithToken(url, auth.token, init);
  if (res.status === 401) {
    const refreshed = await auth.refreshAuth();
    if (refreshed) {
      res = await fetchWithToken(url, refreshed, init);
    }
  }
  if (!res.ok) {
    throw new ApiError(res.status, `HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

function errorMessage(status: number, fallback: string, body?: unknown): string {
  if (body && typeof body === "object") {
    const detail = (body as { detail?: unknown; error?: unknown; message?: unknown }).detail
      ?? (body as { error?: unknown }).error
      ?? (body as { message?: unknown }).message;
    if (typeof detail === "string" && detail.trim()) {
      return `${fallback}: ${detail}`;
    }
  }
  return `${fallback}: HTTP ${status}`;
}

function splitKey(key: string): { channel: string; chatId: string } {
  const idx = key.indexOf(":");
  if (idx === -1) return { channel: "", chatId: key };
  return { channel: key.slice(0, idx), chatId: key.slice(idx + 1) };
}

function stringField(source: Record<string, unknown> | undefined, key: string): string | undefined {
  const value = source?.[key];
  return typeof value === "string" && value.trim() ? value : undefined;
}

function reviewTargetTypeField(value?: string): ReviewTargetType | undefined {
  return value === "auto" || value === "github" || value === "local" ? value : undefined;
}

function reviewActionField(value?: string): ReviewAction | undefined {
  return value === "repo" || value === "diff" ? value : undefined;
}

function reviewDepthField(value?: string): ReviewDepth | undefined {
  return value === "quick" || value === "full" || value === "deep" ? value : undefined;
}

export async function listSessions(auth: ApiAuth): Promise<ChatSummary[]> {
  type Row = {
    key: string;
    created_at: string | null;
    updated_at: string | null;
    title?: string;
    preview?: string;
    metadata?: Record<string, unknown>;
  };

  const body = await request<{ sessions: Row[] }>("/api/sessions", auth);
  return body.sessions.map((session) => {
    const metadata = session.metadata && typeof session.metadata === "object"
      ? session.metadata
      : undefined;
    const reviewTargetType = reviewTargetTypeField(stringField(metadata, "review_target_type"));
    const reviewAction = reviewActionField(stringField(metadata, "review_action"));
    const reviewMode = reviewDepthField(stringField(metadata, "review_mode_variant"));
    return {
      key: session.key,
      ...splitKey(session.key),
      createdAt: session.created_at,
      updatedAt: session.updated_at,
      title: session.title ?? "",
      preview: session.preview ?? "",
      metadata,
      reviewTarget: stringField(metadata, "review_target"),
      reviewTargetType,
      reviewAction,
      reviewMode,
      pinned: metadata?.pinned === true,
      customTitle: stringField(metadata, "custom_title"),
    };
  });
}

export async function fetchReviewerProfiles(auth: ApiAuth): Promise<ReviewerProfile[]> {
  const body = await request<{ profiles: ReviewerProfile[] }>("/api/review/profiles", auth);
  return Array.isArray(body.profiles) ? body.profiles : [];
}

export async function fetchWebuiThread(
  auth: ApiAuth,
  key: string,
): Promise<WebuiThreadPersistedPayload | null> {
  const url = `/api/sessions/${encodeURIComponent(key)}/webui-thread`;
  let res = await fetchWithToken(url, auth.token);
  if (res.status === 401) {
    const refreshed = await auth.refreshAuth();
    if (refreshed) {
      res = await fetchWithToken(url, refreshed);
    }
  }
  if (res.status === 404) return null;
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      // Ignore malformed error bodies; status still carries the failure.
    }
    throw new ApiError(res.status, errorMessage(res.status, "Failed to load webui thread", body));
  }
  return (await res.json()) as WebuiThreadPersistedPayload;
}

export async function fetchSessionMessages(
  auth: ApiAuth,
  key: string,
): Promise<SessionMessagesPayload | null> {
  const url = `/api/sessions/${encodeURIComponent(key)}/messages`;
  let res = await fetchWithToken(url, auth.token);
  if (res.status === 401) {
    const refreshed = await auth.refreshAuth();
    if (refreshed) {
      res = await fetchWithToken(url, refreshed);
    }
  }
  if (res.status === 404) return null;
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      // Ignore malformed error bodies; status still carries the failure.
    }
    throw new ApiError(res.status, errorMessage(res.status, "Failed to load session messages", body));
  }
  return (await res.json()) as SessionMessagesPayload;
}

export async function fetchCodeContext(
  auth: ApiAuth,
  key: string,
  file: string,
  line: number | null | undefined,
  options: { before?: number; after?: number } = {},
): Promise<CodeContextPayload> {
  const params = new URLSearchParams({
    file,
    line: String(line && line > 0 ? line : 1),
    before: String(options.before ?? 8),
    after: String(options.after ?? 12),
  });
  const url = `/api/sessions/${encodeURIComponent(key)}/code-context?${params.toString()}`;
  let res = await fetchWithToken(url, auth.token);
  if (res.status === 401) {
    const refreshed = await auth.refreshAuth();
    if (refreshed) {
      res = await fetchWithToken(url, refreshed);
    }
  }
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      // Keep the status fallback if the server did not return JSON.
    }
    throw new ApiError(res.status, errorMessage(res.status, "Failed to load code context", body));
  }
  return (await res.json()) as CodeContextPayload;
}

export async function deleteSession(auth: ApiAuth, key: string): Promise<boolean> {
  const body = await request<{ deleted: boolean }>(
    `/api/sessions/${encodeURIComponent(key)}/delete`,
    auth,
    { method: "POST" },
  );
  return body.deleted;
}

export async function updateSession(
  auth: ApiAuth,
  key: string,
  updates: { pinned?: boolean; custom_title?: string },
): Promise<boolean> {
  const params = new URLSearchParams();
  if (updates.pinned !== undefined) params.set("pinned", String(updates.pinned));
  if (updates.custom_title !== undefined) params.set("custom_title", updates.custom_title);
  const qs = params.toString();
  const path = `/api/sessions/${encodeURIComponent(key)}/update${qs ? `?${qs}` : ""}`;
  const body = await request<{ updated: boolean }>(path, auth, { method: "GET" });
  return body.updated;
}
