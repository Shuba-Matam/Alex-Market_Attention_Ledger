const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export const TOKEN_KEY = "watch-ledger-token";

export type Session = { user_id: number; username: string; token: string };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((init?.headers as Record<string, string>) ?? {}),
  };
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    const text = await response.text();
    let message = text || `Request failed (${response.status})`;
    try {
      const parsed = JSON.parse(text);
      if (parsed?.detail) message = String(parsed.detail);
    } catch { /* not JSON - show raw text */ }
    const error = new Error(message) as Error & { status?: number };
    error.status = response.status;
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

// Identity comes from the bearer token, so no endpoint takes a username.
export const api = {
  register: (email: string, password: string) =>
    request<Session>("/auth/register", { method: "POST", body: JSON.stringify({ email, password }) }),
  login: (email: string, password: string) =>
    request<Session>("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  loginWithGoogle: (credential: string) =>
    request<Session>("/auth/google", { method: "POST", body: JSON.stringify({ credential }) }),
  loginDemo: (username: string) =>
    request<Session>("/auth/demo", { method: "POST", body: JSON.stringify({ username }) }),
  authConfig: () => request<{ google_client_id: string | null }>("/auth/config"),
  users: () => request<{ users: string[] }>("/users"),
  stats: () => request<{ unique_symbols: number; users: number }>("/stats"),
  watchlist: () => request<unknown>("/watchlist"),
  addSymbol: (symbol: string) => request<unknown>("/watchlist/symbols", { method: "POST", body: JSON.stringify({ symbol }) }),
  removeSymbol: (symbol: string) => request<unknown>(`/watchlist/symbols/${encodeURIComponent(symbol)}`, { method: "DELETE" }),
  markSeen: () => request<unknown>("/watchlist/seen", { method: "POST" }),
  tick: () => request<unknown>("/poll", { method: "POST", body: JSON.stringify({}) }),
};