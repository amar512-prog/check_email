import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import {
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  ChevronsUpDown,
  CircleAlert,
  Download,
  FileSpreadsheet,
  LogOut,
  MailCheck,
  RefreshCw,
  Server,
  Upload,
  X,
} from "lucide-react";
import { api } from "./api";
import type { Job, PublicConfig, Reachability, ReacherResult, ResultSort, User } from "./types";

const filters: Array<{ value: "all" | Reachability; label: string }> = [
  { value: "all", label: "All" },
  { value: "safe", label: "Safe" },
  { value: "risky", label: "Risky" },
  { value: "invalid", label: "Invalid" },
  { value: "unknown", label: "Unknown" },
];

const RESULTS_PAGE_SIZE = 50;
const JOBS_PAGE_SIZE = 50;

function Pager({ offset, pageSize, total, onChange }: {
  offset: number;
  pageSize: number;
  total: number;
  onChange: (offset: number) => void;
}) {
  if (total === 0) return null;
  const from = offset + 1;
  const to = Math.min(offset + pageSize, total);
  const atStart = offset <= 0;
  const atEnd = to >= total;
  return (
    <div className="pager">
      <span className="pager-count">Showing {from.toLocaleString()}–{to.toLocaleString()} of {total.toLocaleString()}</span>
      <div className="pager-buttons">
        <button className="secondary-button" disabled={atStart} onClick={() => onChange(Math.max(0, offset - pageSize))}>
          <ChevronLeft size={16} /> Prev
        </button>
        <button className="secondary-button" disabled={atEnd} onClick={() => onChange(offset + pageSize)}>
          Next <ChevronRight size={16} />
        </button>
      </div>
    </div>
  );
}

function GoogleLogin({ config, onLogin }: { config: PublicConfig; onLogin: (user: User) => void }) {
  const target = useRef<HTMLDivElement>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (config.auth_mode !== "google") return;
    const initialize = () => {
      if (!window.google || !target.current) return;
      window.google.accounts.id.initialize({
        client_id: config.google_client_id,
        callback: async ({ credential }) => {
          try {
            const response = await api.googleLogin(credential);
            onLogin(response.user);
          } catch (loginError) {
            setError(loginError instanceof Error ? loginError.message : "Google login failed");
          }
        },
        auto_select: false,
        cancel_on_tap_outside: true,
      });
      window.google.accounts.id.renderButton(target.current, {
        type: "standard",
        theme: "outline",
        size: "large",
        text: "continue_with",
        shape: "rectangular",
        width: 320,
      });
    };

    const existing = document.querySelector<HTMLScriptElement>("script[data-google-identity]");
    if (existing) {
      if (window.google) initialize();
      else existing.addEventListener("load", initialize, { once: true });
      return;
    }
    const script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.async = true;
    script.defer = true;
    script.dataset.googleIdentity = "true";
    script.addEventListener("load", initialize, { once: true });
    document.head.appendChild(script);
  }, [config, onLogin]);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const developmentLogin = async () => {
    try {
      setError("");
      const response = await api.developmentLogin();
      onLogin(response.user);
    } catch (loginError) {
      setError(loginError instanceof Error ? loginError.message : "Login failed");
    }
  };

  const passwordLogin = async (event: FormEvent) => {
    event.preventDefault();
    try {
      setError("");
      setSubmitting(true);
      const response = await api.passwordLogin(username, password);
      onLogin(response.user);
    } catch (loginError) {
      setError(loginError instanceof Error ? loginError.message : "Login failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="login-shell">
      <section className="login-panel" aria-labelledby="login-title">
        <div className="brand-mark"><MailCheck size={24} aria-hidden="true" /></div>
        <h1 id="login-title">Mailcheck</h1>
        <p>Sign in to verify email lists across your connected servers.</p>
        {config.auth_mode === "google" ? (
          <div ref={target} className="google-button" />
        ) : (
          <button className="google-fallback" onClick={developmentLogin}>
            <span className="google-g">G</span>
            Continue locally
          </button>
        )}
        {config.password_enabled && (
          <>
            <div className="login-divider"><span>or</span></div>
            <form className="credential-form" onSubmit={passwordLogin}>
              <input
                type="text"
                placeholder="Username"
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
              />
              <input
                type="password"
                placeholder="Password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
              <button type="submit" className="primary-button" disabled={!username || !password || submitting}>
                Sign in
              </button>
            </form>
          </>
        )}
        {error && <p className="form-error">{error}</p>}
      </section>
    </main>
  );
}

function AppHeader({ user, onLogout }: { user: User; onLogout: () => void }) {
  return (
    <header className="app-header">
      <div className="header-inner">
        <a className="wordmark" href="/" aria-label="Mailcheck home">
          <MailCheck size={22} aria-hidden="true" />
          <span>Mailcheck</span>
        </a>
        <div className="header-account">
          <span className="health-dot" aria-hidden="true" />
          <span className="health-label">Coordinator online</span>
          <div className="account-divider" />
          {user.picture ? <img src={user.picture} alt="" /> : <span className="avatar">{user.name[0]}</span>}
          <div className="account-copy">
            <strong>{user.name}</strong>
            <span>{user.email}</span>
          </div>
          <button className="icon-button" onClick={onLogout} title="Sign out" aria-label="Sign out">
            <LogOut size={18} />
          </button>
        </div>
      </div>
    </header>
  );
}

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function parseManualEmails(text: string): { valid: string[]; invalid: number } {
  const seen = new Set<string>();
  let invalid = 0;
  for (const token of text.split(/[\s,;]+/)) {
    const value = token.trim().toLowerCase();
    if (!value) continue;
    if (!EMAIL_PATTERN.test(value)) {
      invalid += 1;
      continue;
    }
    seen.add(value);
  }
  return { valid: [...seen], invalid };
}

function UploadWorkspace({
  config,
  onCreated,
}: {
  config: PublicConfig;
  onCreated: (jobId: string) => void;
}) {
  const [mode, setMode] = useState<"csv" | "manual">("csv");
  const [file, setFile] = useState<File | null>(null);
  const [manualText, setManualText] = useState("");
  const [retryDelay, setRetryDelay] = useState(1);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const online = config.servers.filter((server) => server.status === "online");
  const capacity = online.reduce((sum, server) => sum + server.emails_per_minute, 0);
  const manual = parseManualEmails(manualText);
  const ready = mode === "csv" ? !!file : manual.valid.length > 0;

  const selectFile = (next: File | undefined) => {
    setError("");
    if (!next) return;
    if (!next.name.toLowerCase().endsWith(".csv")) {
      setError("Choose a CSV file.");
      return;
    }
    setFile(next);
  };

  const submit = async () => {
    if (!ready || !online.length) return;
    try {
      setError("");
      setSubmitting(true);
      const created =
        mode === "csv" && file
          ? await api.createJob(file, retryDelay)
          : await api.createJobFromEmails(manual.valid, retryDelay);
      onCreated(created.job_id);
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "Could not create the job");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="workspace-grid" aria-label="Create verification job">
      <div className="upload-region">
        <div className="mode-tabs" role="tablist" aria-label="Input method">
          <button
            role="tab"
            aria-selected={mode === "csv"}
            className={mode === "csv" ? "active" : ""}
            onClick={() => { setMode("csv"); setError(""); }}
          >Upload CSV</button>
          <button
            role="tab"
            aria-selected={mode === "manual"}
            className={mode === "manual" ? "active" : ""}
            onClick={() => { setMode("manual"); setError(""); }}
          >Enter emails</button>
        </div>
        {mode === "csv" ? (
          <div
            className={`drop-zone ${dragging ? "is-dragging" : ""} ${file ? "has-file" : ""}`}
            onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              selectFile(event.dataTransfer.files[0]);
            }}
          >
            <input
              ref={input}
              type="file"
              accept=".csv,text/csv"
              onChange={(event) => selectFile(event.target.files?.[0])}
            />
            {file ? (
              <>
                <FileSpreadsheet size={30} aria-hidden="true" />
                <strong>{file.name}</strong>
                <span>{(file.size / 1024).toFixed(1)} KB</span>
                <button className="icon-button clear-file" onClick={() => setFile(null)} title="Remove file" aria-label="Remove file">
                  <X size={17} />
                </button>
              </>
            ) : (
              <>
                <Upload size={30} aria-hidden="true" />
                <strong>Upload CSV</strong>
                <span>First column must contain email addresses</span>
                <button className="secondary-button" onClick={() => input.current?.click()}>Choose file</button>
              </>
            )}
          </div>
        ) : (
          <div className="manual-entry">
            <textarea
              value={manualText}
              onChange={(event) => { setManualText(event.target.value); setError(""); }}
              placeholder={"amar@basisvps.com\njane@example.com\n\nSeparate addresses with new lines, commas, spaces, or semicolons."}
              aria-label="Email addresses to verify"
              spellCheck={false}
            />
            <p className="manual-hint">
              {manual.valid.length
                ? `${manual.valid.length} unique email${manual.valid.length === 1 ? "" : "s"} detected`
                : "One address is enough — paste a whole list to spread it across all servers."}
              {manual.invalid > 0 && ` · ${manual.invalid} entr${manual.invalid === 1 ? "y" : "ies"} ignored as invalid`}
            </p>
          </div>
        )}
        {error && <p className="form-error"><CircleAlert size={15} /> {error}</p>}
      </div>

      <aside className="server-panel">
        <div className="section-heading compact">
          <div>
            <h2>Connected servers</h2>
            <p>{capacity} emails per minute configured</p>
          </div>
          <Server size={20} aria-hidden="true" />
        </div>
        <div className="server-list">
          {config.servers.map((server) => (
            <div className="server-row" key={server.name}>
              <span className={`status-indicator ${server.status}`} aria-hidden="true" />
              <div>
                <strong>{server.name}</strong>
                <span>{server.status === "online" ? `v${server.version}` : "Unavailable"}</span>
              </div>
              <span className="server-rate">{server.emails_per_minute}/min</span>
            </div>
          ))}
        </div>
        <label className="retry-field">
          <span>Retry unknowns after</span>
          <span className="retry-input">
            <input
              type="number"
              min={1}
              max={15}
              value={retryDelay}
              onChange={(event) => {
                const value = Number(event.target.value);
                if (Number.isFinite(value)) setRetryDelay(Math.max(1, Math.min(15, Math.round(value))));
              }}
            />
            min
          </span>
        </label>
        <button className="primary-button" disabled={!ready || !online.length || submitting} onClick={submit}>
          {submitting ? <RefreshCw className="spin" size={18} /> : <Check size={18} />}
          Start verification
        </button>
      </aside>
    </section>
  );
}

function JobsList({ jobs, total, offset, onSelect, onPage }: {
  jobs: Job[];
  total: number;
  offset: number;
  onSelect: (jobId: string) => void;
  onPage: (offset: number) => void;
}) {
  return (
    <section className="jobs-section" aria-label="All verification jobs">
      <div className="section-heading">
        <div>
          <h2>Recent jobs</h2>
          <p>Jobs from every user. Click a row to open its results.</p>
        </div>
      </div>
      <div className="table-scroll">
        <table className="jobs-table">
          <thead>
            <tr>
              <th>Job</th><th>Owner</th><th>Status</th><th>Progress</th>
              <th>Safe</th><th>Risky</th><th>Invalid</th><th>Unknown</th><th>Created</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr key={job.id} className="job-row" onClick={() => onSelect(job.id)}>
                <td className="email-cell">{job.filename}</td>
                <td>{job.user_email}</td>
                <td><span className={`job-state ${job.status}`}>{job.status}</span></td>
                <td>{job.processed.toLocaleString()}/{job.total.toLocaleString()}</td>
                <td>{job.safe.toLocaleString()}</td>
                <td>{job.risky.toLocaleString()}</td>
                <td>{job.invalid.toLocaleString()}</td>
                <td>{job.unknown.toLocaleString()}</td>
                <td>{new Date(job.created_at).toLocaleString()}</td>
              </tr>
            ))}
            {!jobs.length && <tr><td colSpan={9} className="empty-row">No jobs yet. Upload a CSV or enter emails above.</td></tr>}
          </tbody>
        </table>
      </div>
      <Pager offset={offset} pageSize={JOBS_PAGE_SIZE} total={total} onChange={onPage} />
    </section>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className={`stat ${tone || ""}`}>
      <span>{label}</span>
      <strong>{value.toLocaleString()}</strong>
    </div>
  );
}

function JobProgress({ job }: { job: Job }) {
  const percent = job.total ? Math.min(100, Math.round((job.processed / job.total) * 100)) : 0;
  return (
    <section className="job-section">
      <div className="section-heading">
        <div>
          <h2>Job progress</h2>
          <p>{job.filename} · {job.status}</p>
        </div>
        <span className={`job-state ${job.status}`}>{job.status}</span>
      </div>
      <div className="stats-row">
        <Stat label="Processed" value={job.processed} />
        <Stat label="Safe" value={job.safe} tone="safe" />
        <Stat label="Risky" value={job.risky} tone="risky" />
        <Stat label="Invalid" value={job.invalid} tone="invalid" />
        <Stat label="Unknown" value={job.unknown} tone="unknown" />
      </div>
      <div className="progress-line" aria-label={`${percent}% complete`}>
        <span style={{ width: `${percent}%` }} />
      </div>
      <div className="progress-meta"><span>{percent}% complete</span><span>{job.processed.toLocaleString()} of {job.total.toLocaleString()}</span></div>
      {job.status === "retrying" && (
        <p className="retry-note">Re-checking {job.unknown.toLocaleString()} unknown result{job.unknown === 1 ? "" : "s"}…</p>
      )}
      {!!job.servers?.length && (
        <div className="allocation-table">
          {job.servers.map((server) => (
            <div className="allocation-row" key={server.server_name}>
              <span className="status-indicator online" aria-hidden="true" />
              <strong>{server.server_name}</strong>
              <span>{server.completed_batches}/{server.batches} batches</span>
              <span>{server.processed}/{server.total} emails</span>
            </div>
          ))}
        </div>
      )}
      {job.error && <p className="form-error"><CircleAlert size={15} /> {job.error}</p>}
    </section>
  );
}

function ResultStatus({ status }: { status: Reachability }) {
  return <span className={`result-status ${status}`}><span />{status}</span>;
}

function ResultsTable({ job, results, total, offset, filter, sort, onFilter, onPage, onSort }: {
  job: Job;
  results: ReacherResult[];
  total: number;
  offset: number;
  filter: string;
  sort: ResultSort;
  onFilter: (filter: string) => void;
  onPage: (offset: number) => void;
  onSort: (sort: ResultSort) => void;
}) {
  const nextSort: ResultSort = sort === "default" ? "email_asc" : sort === "email_asc" ? "email_desc" : "default";
  const sortLabel = sort === "email_asc" ? "ascending" : sort === "email_desc" ? "descending" : "unsorted";
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState("");
  const duration = (result: ReacherResult) => {
    const value = result.debug?.duration;
    return ((value?.secs || 0) + (value?.nanos || 0) / 1_000_000_000).toFixed(2);
  };

  const downloadCsv = async () => {
    try {
      setDownloading(true);
      setDownloadError("");
      const blob = await api.downloadResults(job.id);
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = `mailcheck-${job.id}.csv`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch (error) {
      setDownloadError(error instanceof Error ? error.message : "Could not download results");
    } finally {
      setDownloading(false);
    }
  };

  return (
    <section className="results-section">
      <div className="section-heading results-heading">
        <div><h2>Results</h2><p>Verified records, newest first</p></div>
        <button className="secondary-button download-button" onClick={downloadCsv} disabled={downloading || !results.length}>
          {downloading ? <RefreshCw className="spin" size={17} /> : <Download size={17} />}
          {downloading ? "Preparing CSV" : "Download CSV"}
        </button>
      </div>
      {downloadError && <p className="form-error download-error"><CircleAlert size={15} /> {downloadError}</p>}
      <div className="filter-tabs" role="tablist" aria-label="Filter results">
        {filters.map((item) => (
          <button
            key={item.value}
            role="tab"
            aria-selected={filter === item.value}
            className={filter === item.value ? "active" : ""}
            onClick={() => onFilter(item.value)}
          >{item.label}</button>
        ))}
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>
                <button
                  type="button"
                  className={`sort-header ${sort !== "default" ? "active" : ""}`}
                  onClick={() => onSort(nextSort)}
                  aria-label={`Sort by email (currently ${sortLabel})`}
                >
                  Email
                  {sort === "email_asc" ? <ChevronUp size={14} /> : sort === "email_desc" ? <ChevronDown size={14} /> : <ChevronsUpDown size={14} />}
                </button>
              </th>
              <th>Status</th><th>MX</th><th>SMTP</th><th>Catch-all</th><th>Duration</th>
            </tr>
          </thead>
          <tbody>
            {results.map((result, index) => (
              <tr key={`${result.input}-${index}`}>
                <td className="email-cell">{result.input}</td>
                <td><ResultStatus status={result.is_reachable} /></td>
                <td>{result.mx?.accepts_mail ? "Accepts mail" : "No"}</td>
                <td>{result.smtp?.is_deliverable ? "Deliverable" : "No"}</td>
                <td>{result.smtp?.is_catch_all ? "Yes" : "No"}</td>
                <td>{duration(result)}s</td>
              </tr>
            ))}
            {!results.length && <tr><td colSpan={6} className="empty-row">Results will appear as batches complete.</td></tr>}
          </tbody>
        </table>
      </div>
      <Pager offset={offset} pageSize={RESULTS_PAGE_SIZE} total={total} onChange={onPage} />
    </section>
  );
}

export function App() {
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [job, setJob] = useState<Job | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobsOffset, setJobsOffset] = useState(0);
  const [results, setResults] = useState<ReacherResult[]>([]);
  const [resultsTotal, setResultsTotal] = useState(0);
  const [resultsOffset, setResultsOffset] = useState(0);
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState<ResultSort>("default");
  const [error, setError] = useState("");

  const bootstrap = useCallback(async () => {
    try {
      const [nextConfig, session] = await Promise.all([api.config(), api.me()]);
      setConfig(nextConfig);
      setUser(session.user);
    } catch (bootstrapError) {
      setError(bootstrapError instanceof Error ? bootstrapError.message : "Could not load Mailcheck");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void bootstrap(); }, [bootstrap]);

  useEffect(() => {
    if (!user || job) return;
    let cancelled = false;
    const refreshJobs = async () => {
      try {
        const listing = await api.jobs(JOBS_PAGE_SIZE, jobsOffset);
        if (cancelled) return;
        setJobs(listing.jobs);
        setJobsTotal(listing.total);
      } catch {
        // Listing refresh is best-effort; job view errors surface elsewhere.
      }
    };
    void refreshJobs();
    const timer = window.setInterval(refreshJobs, 5000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [user, job, jobsOffset]);

  useEffect(() => {
    if (!job || !user) return;
    let cancelled = false;
    const refresh = async () => {
      try {
        const [nextJob, nextResults] = await Promise.all([
          api.job(job.id),
          api.results(job.id, filter, RESULTS_PAGE_SIZE, resultsOffset, sort),
        ]);
        if (cancelled) return;
        setJob(nextJob);
        setResults(nextResults.results);
        setResultsTotal(nextResults.total);
      } catch (refreshError) {
        if (!cancelled) setError(refreshError instanceof Error ? refreshError.message : "Could not refresh the job");
      }
    };
    void refresh();
    if (job.status === "completed" || job.status === "failed") return () => { cancelled = true; };
    const timer = window.setInterval(refresh, 1000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [job?.id, job?.status, filter, sort, resultsOffset, user]);

  const openJob = async (jobId: string) => {
    setFilter("all");
    setSort("default");
    setResults([]);
    setResultsTotal(0);
    setResultsOffset(0);
    setJob(await api.job(jobId));
  };

  const logout = async () => {
    await api.logout();
    setUser(null);
    setJob(null);
    setResults([]);
  };

  if (loading) return <div className="loading-screen"><RefreshCw className="spin" size={24} /></div>;
  if (error && !config) return <div className="fatal-error"><CircleAlert size={24} /><strong>Mailcheck could not start</strong><span>{error}</span></div>;
  if (!config) return null;
  if (!user) return <GoogleLogin config={config} onLogin={setUser} />;

  return (
    <>
      <AppHeader user={user} onLogout={logout} />
      <main className="app-main">
        <div className="page-heading">
          <div><h1>Verify email lists</h1><p>Upload a CSV or enter addresses directly; verification is distributed across connected workers.</p></div>
          {job && (
            <button className="text-button" onClick={() => { setJob(null); setResults([]); setResultsTotal(0); setResultsOffset(0); setFilter("all"); setSort("default"); }}>
              New job
            </button>
          )}
        </div>
        {!job ? (
          <>
            <UploadWorkspace config={config} onCreated={openJob} />
            <JobsList jobs={jobs} total={jobsTotal} offset={jobsOffset} onSelect={openJob} onPage={setJobsOffset} />
          </>
        ) : (
          <>
            <JobProgress job={job} />
            <ResultsTable
              job={job}
              results={results}
              total={resultsTotal}
              offset={resultsOffset}
              filter={filter}
              sort={sort}
              onFilter={(next) => { setFilter(next); setResultsOffset(0); }}
              onPage={setResultsOffset}
              onSort={(next) => { setSort(next); setResultsOffset(0); }}
            />
          </>
        )}
        {error && job && <button className="toast" onClick={() => setError("")}><CircleAlert size={16} />{error}<X size={15} /></button>}
      </main>
    </>
  );
}
