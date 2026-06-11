export type Reachability = "safe" | "risky" | "invalid" | "unknown";

export type ResultSort = "default" | "email_asc" | "email_desc";

export interface User {
  sub: string;
  email: string;
  name: string;
  picture: string;
}

export interface ServerHealth {
  name: string;
  status: "online" | "offline";
  version: string | null;
  emails_per_minute: number;
}

export interface PublicConfig {
  auth_mode: "development" | "google";
  google_client_id: string;
  password_enabled: boolean;
  max_upload_emails: number;
  servers: ServerHealth[];
}

export interface ServerProgress {
  server_name: string;
  total: number;
  processed: number;
  batches: number;
  completed_batches: number;
  error: string | null;
}

export interface Job {
  id: string;
  filename: string;
  user_email: string;
  status: "queued" | "running" | "retrying" | "completed" | "failed";
  total: number;
  processed: number;
  safe: number;
  risky: number;
  invalid: number;
  unknown: number;
  error: string | null;
  created_at: string;
  updated_at: string;
  servers?: ServerProgress[];
}

export interface ReacherResult {
  input: string;
  is_reachable: Reachability;
  mx?: { accepts_mail?: boolean; records?: string[] };
  smtp?: { is_deliverable?: boolean; is_catch_all?: boolean };
  debug?: { duration?: { secs?: number; nanos?: number } };
}

