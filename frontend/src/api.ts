const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `Request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  watchlist: (username: string) => request<unknown>(`/watchlist/${encodeURIComponent(username)}`),
  addSymbol: (username: string, symbol: string) => request<unknown>(`/watchlist/${encodeURIComponent(username)}/symbols`, { method: "POST", body: JSON.stringify({ symbol }) }),
  removeSymbol: (username: string, symbol: string) => request<unknown>(`/watchlist/${encodeURIComponent(username)}/symbols/${encodeURIComponent(symbol)}`, { method: "DELETE" }),
  markSeen: (username: string) => request<unknown>(`/watchlist/${encodeURIComponent(username)}/seen`, { method: "POST" }),
  tick: () => request<unknown>("/poll", { method: "POST", body: JSON.stringify({}) }),
};
