import type { Job, PublicConfig, ReacherResult, User } from "./types";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...options?.headers,
    },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail || `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

async function download(path: string): Promise<Blob> {
  const response = await fetch(path, { credentials: "same-origin" });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail || `Download failed (${response.status})`);
  }
  return response.blob();
}

export const api = {
  config: () => request<PublicConfig>("/api/config"),
  me: () => request<{ user: User | null }>("/api/auth/me"),
  developmentLogin: () => request<{ user: User }>("/api/auth/development", { method: "POST" }),
  googleLogin: (credential: string) =>
    request<{ user: User }>("/api/auth/google", {
      method: "POST",
      body: JSON.stringify({ credential }),
    }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  createJob: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<{ job_id: string; accepted: number; rejected: number }>("/api/jobs", {
      method: "POST",
      body,
    });
  },
  createJobFromEmails: (emails: string[]) =>
    request<{ job_id: string; accepted: number; rejected: number }>("/api/jobs/emails", {
      method: "POST",
      body: JSON.stringify({ emails }),
    }),
  jobs: () => request<{ jobs: Job[] }>("/api/jobs"),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  results: (id: string, status: string) =>
    request<{ total: number; results: ReacherResult[] }>(
      `/api/jobs/${id}/results?status=${encodeURIComponent(status)}&limit=200`,
    ),
  downloadResults: (id: string) => download(`/api/jobs/${id}/download`),
};
