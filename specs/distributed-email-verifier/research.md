# Verifiable Research and Technology Proposal

## 1. Core Problem Analysis

The proposed system accepts a CSV containing one email address per row and
distributes verification work across four independent Reacher servers. Each
server must start no more than 50 verification requests per minute.

The browser must not call the four Reacher servers directly. Direct browser
dispatch would expose each server's `x-reacher-secret`, make retries unreliable
when the browser closes, and provide no durable record of partial progress. The
browser should upload the CSV to a central application backend. That backend
should parse and persist the batch, enqueue each row, dispatch work to the four
servers, store results, and provide progress and CSV download endpoints.

Reacher is stateless and supports horizontal deployment, but its documentation
states that coordination is still desirable for throttling and proxy/IP limits.
It also provides server-side throttling for `/v1/*` endpoints; `/v0/check_email`
executes immediately and ignores the configured throttle. [cite:1] [cite:2]

### Capacity model

- Hard start-rate ceiling: `4 servers * 50 requests/minute = 200 requests/minute`.
- Theoretical completion time: 1,000 rows in 5 minutes; 10,000 rows in 50
  minutes; 40,000 rows in 200 minutes.
- Actual throughput is lower when SMTP checks are slow. Approximate per-node
  throughput is `min(50, concurrency * 60 / average_request_seconds)`.
- With concurrency 5, a 6-second average request can reach 50/minute, while a
  10-second average produces about 30/minute.
- Reacher recommends both per-minute and per-day limits, with documented
  defaults of 60/minute and 10,000/day. At 50/minute continuously, one server
  reaches 10,000 requests in 3 hours 20 minutes. Four servers therefore have a
  recommended initial daily ceiling of 40,000, not 288,000. [cite:2]

The 50/minute value should be treated as a maximum request start rate, not a
promise of 50 completed results per minute. Start with concurrency 5 per server,
measure latency and provider errors, and increase only after observing stable IP
reputation and SMTP outcomes.

## 2. Verifiable Technology Recommendations

| Technology/Pattern | Rationale & Evidence |
|---|---|
| **Central Coordinator Backend** | Keep CSV processing, Reacher credentials, retries, and progress state on a trusted server. Reacher uses a shared secret in the `x-reacher-secret` header, so those secrets must never be delivered to browser JavaScript. Reacher documents `RCH__HEADER_SECRET` for this purpose. [cite:2] OWASP recommends centralized secret storage and runtime injection instead of hardcoding secrets. [cite:5] |
| **Four independent queues** | Create one queue per Reacher node. This makes the 50/minute limit and concurrency independently enforceable for every server and prevents a fast node from consuming another node's allowance. BullMQ provides queue-level rate limiting; limited jobs remain waiting rather than being discarded. [cite:3] |
| **Smoothed rate limit** | Configure each node queue to start one request every 1,200 ms, equivalent to 50/minute, instead of releasing a burst of 50 at the start of a minute. Also configure Reacher itself with `RCH__THROTTLE__MAX_REQUESTS_PER_MINUTE=50` as a defensive second limit. Reacher's throttle applies only to `/v1/*`. [cite:2] |
| **Bounded asynchronous concurrency** | Use an initial concurrency of 5 per node. HTTP/SMTP waiting is asynchronous work, and BullMQ supports multiple concurrent asynchronous jobs in a worker. [cite:4] |
| **`POST /v1/check_email`** | Use `/v1/check_email`, not `/v0/check_email`, because `/v1` honors Reacher's throttle settings. [cite:2] |
| **PostgreSQL durable state** | Persist batches, input rows, assignments, attempts, full Reacher responses, and final classifications. The queue is for execution; PostgreSQL is the source of truth for user-visible progress and downloadable results. This is an architectural recommendation, not a Reacher requirement. |
| **Redis + BullMQ execution queue** | Recommended for a TypeScript implementation because BullMQ directly supports queue rate limits and asynchronous worker concurrency. [cite:3] [cite:4] A Python implementation could use an equivalent durable queue, but must preserve the same per-node limiter semantics. |
| **Private node connectivity** | Prefer WireGuard/Tailscale/private networking between the Coordinator Backend and Reacher nodes. If public HTTPS is required, use Nginx, TLS, source-IP allowlisting, and a unique `x-reacher-secret` per node. Reacher should remain bound to localhost behind the proxy. |
| **Runtime secret injection** | Store four different Reacher secrets in the coordinator's secret store or protected runtime configuration. Do not place secrets in the CSV, frontend bundle, Git, logs, or downloadable output. OWASP warns against hardcoded container secrets and recommends deployment-time injection. [cite:5] |
| **Fixed Reacher image** | Pin the tested Docker digest instead of `latest` on every server, ensuring all four nodes execute the same Reacher build. Upgrade all nodes deliberately after a canary test. |

## 3. Proposed Request Lifecycle

1. User uploads a CSV to the web application.
2. The Coordinator Backend streams the file, accepts one email column, trims
   whitespace, rejects empty rows, and stores the original row order.
3. Exact duplicate rows are marked as duplicates or linked to one canonical
   verification, depending on the chosen product behavior.
4. Valid rows enter a central pending pool.
5. The Dispatcher assigns pending rows to the least-loaded healthy node queue.
6. Each node queue starts no more than one request per 1,200 ms and no more than
   five concurrent requests initially.
7. The worker calls the assigned node's `/v1/check_email` endpoint using that
   node's private URL and secret.
8. The result and raw response are committed to PostgreSQL before the queue job
   is acknowledged.
9. The frontend polls or subscribes to batch progress and displays counts for
   queued, processing, safe, risky, invalid, unknown, and failed rows.
10. The user downloads a result CSV preserving the original input order.

## 4. Failure and Retry Policy

- Network timeout or HTTP 5xx: retry up to three times with exponential backoff.
- HTTP 429: delay the job until the node's next available rate window; do not
  count it as a permanent failure.
- HTTP 400: mark the row failed without retry unless the failure is caused by a
  correctable payload issue.
- Node unavailable: open a circuit breaker after repeated failures and reassign
  queued jobs to another healthy node through that destination node's limiter.
- Coordinator restart: recover unfinished rows from PostgreSQL/Redis without
  duplicating completed results.
- Request timeout: begin with 60 seconds at the coordinator and configure a
  finite Reacher SMTP timeout; record timeouts separately from invalid emails.

## 5. Deployment Shape

### Recommended production topology

- **Web/Coordinator server**: frontend, backend API, dispatcher, queue workers.
- **PostgreSQL**: durable batch and result data.
- **Redis**: four execution queues and limiter state.
- **Reacher Node 1-4**: fixed Docker image, unique secret, `/v1` throttle set to
  50/minute, daily cap set initially to 10,000, outbound TCP 25 or approved SMTP
  proxy, health endpoint available only to the coordinator.

The Web/Coordinator server may initially host PostgreSQL and Redis for an MVP,
but production backups and resource monitoring are required. Do not run the
browser as the scheduler.

## 6. Product and Operational Decisions Still Needed

1. Maximum CSV size or row count per upload.
2. Whether duplicate emails should consume one verification or preserve one
   verification per input row.
3. Required result retention period and deletion controls.
4. Whether users need accounts, multi-tenancy, or only a single internal login.
5. Daily target volume. More than 40,000/day conflicts with Reacher's documented
   initial 10,000/day-per-node guidance and requires an explicit IP/proxy plan.
6. Whether this is a proprietary/commercial application. Reacher documents a
   dual-license model: commercial proprietary tools require the commercial
   license, while AGPL-compatible open-source applications can use AGPL terms.
   Confirm licensing before implementation. [cite:6]

## 7. Browsed Sources

- [1] https://docs.reacher.email/self-hosting/scaling-for-production - Reacher horizontal scaling and high-volume guidance.
- [2] https://docs.reacher.email/self-hosting/reacher-configuration-v0.10 - Reacher 0.11 authentication, throttle, daily limits, worker concurrency, and `/v1` behavior.
- [3] https://docs.bullmq.io/guide/rate-limiting - BullMQ queue rate limiting and waiting behavior.
- [4] https://docs.bullmq.io/guide/workers/concurrency - BullMQ asynchronous concurrency and worker behavior.
- [5] https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html - Runtime secret management guidance.
- [6] https://github.com/reacherhq/check-if-email-exists#license - Reacher dual-license terms and high-volume proxy note.
