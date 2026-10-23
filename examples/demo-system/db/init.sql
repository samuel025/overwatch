-- Demo system databases. Two separate databases on purpose: application data (shopdb)
-- and operational data (opsdb). Overwatch (via Zetaris) has to correlate across both.

CREATE ROLE overwatch_ro LOGIN PASSWORD 'overwatch_ro';
GRANT pg_read_all_stats TO overwatch_ro;

CREATE DATABASE shopdb;
CREATE DATABASE opsdb;

\c shopdb

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE customers (
  id serial PRIMARY KEY,
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE orders (
  id bigserial PRIMARY KEY,
  customer_id int NOT NULL,
  amount numeric(10, 2) NOT NULL,
  status text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE payments (
  id bigserial PRIMARY KEY,
  order_id bigint NOT NULL,
  amount numeric(10, 2) NOT NULL,
  status text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO customers (name)
SELECT 'customer ' || g FROM generate_series(1, 50000) g;

-- Enough rows that losing the customer_id index turns a 1 ms lookup into a slow sequential scan.
INSERT INTO orders (customer_id, amount, status, created_at)
SELECT (random() * 49999)::int + 1,
       round((random() * 200 + 5)::numeric, 2),
       (ARRAY['paid', 'shipped', 'delivered', 'refunded'])[1 + (random() * 3)::int],
       now() - (random() * interval '365 days')
FROM generate_series(1, 3000000);

CREATE INDEX orders_customer_id_idx ON orders (customer_id);
ANALYZE;

GRANT CONNECT ON DATABASE shopdb TO overwatch_ro;
GRANT USAGE ON SCHEMA public TO overwatch_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO overwatch_ro;

\c opsdb

CREATE TABLE request_metrics (
  ts timestamptz NOT NULL,
  service text NOT NULL,
  instance text NOT NULL DEFAULT '',
  endpoint text NOT NULL,
  requests int NOT NULL,
  rps float NOT NULL,
  p50_ms float NOT NULL,
  p95_ms float NOT NULL,
  error_rate float NOT NULL
);
CREATE INDEX ON request_metrics (service, ts);

CREATE TABLE app_logs (
  ts timestamptz NOT NULL DEFAULT now(),
  service text NOT NULL,
  instance text NOT NULL DEFAULT '',
  level text NOT NULL,
  message text NOT NULL
);
CREATE INDEX ON app_logs (service, ts);

-- Container-level metrics, like cAdvisor / kubelet would provide.
CREATE TABLE container_metrics (
  ts timestamptz NOT NULL,
  service text NOT NULL,
  instance text NOT NULL,
  cpu_pct float NOT NULL,           -- percent of one core
  cpu_limit_cores float,
  mem_mb float NOT NULL,
  mem_limit_mb float,
  restarts int NOT NULL,            -- cumulative restart count
  oom_killed boolean NOT NULL,      -- last exit was an out-of-memory kill
  status text NOT NULL
);
CREATE INDEX ON container_metrics (service, ts);

CREATE TABLE deploys (
  ts timestamptz NOT NULL DEFAULT now(),
  service text NOT NULL,
  version text NOT NULL,
  author text NOT NULL,
  description text NOT NULL
);

-- Ticket history (what a Jira export would look like).
CREATE TABLE tickets (
  key text PRIMARY KEY,
  created timestamptz NOT NULL,
  title text NOT NULL,
  status text NOT NULL,
  resolution text
);

INSERT INTO deploys (ts, service, version, author, description) VALUES
  (now() - interval '9 days', 'orders',   'v1.7.0', 'maria', 'Release 1.7.0'),
  (now() - interval '6 days', 'payments', 'v2.2.0', 'dev-team', 'Release 2.2.0'),
  (now() - interval '2 days', 'gateway',  'v3.1.4', 'li', 'Release 3.1.4');

INSERT INTO tickets (key, created, title, status, resolution) VALUES
  ('OPS-101', now() - interval '40 days', 'orders p95 latency 2.1s after release', 'Done',
   'Migration in orders v1.4 dropped orders_customer_id_idx; lookups by customer did full scans. Recreated the index.'),
  ('OPS-117', now() - interval '25 days', 'payments returning 500s after deploy', 'Done',
   'payments v2.0 shipped a validation bug rejecting valid cards. Rolled back to v1.9.'),
  ('OPS-122', now() - interval '18 days', 'checkout timeouts, gateway errors', 'Done',
   'payments provider sandbox slow (3s+); gateway 1s timeout to payments fired. Added retry with backoff.'),
  ('OPS-130', now() - interval '11 days', 'payments intermittent errors "pool timeout"', 'Done',
   'Config change reduced payments DB pool to 2 while slow statements held connections. Restored pool size 10.'),
  ('OPS-141', now() - interval '5 days', 'everything slow mid-morning, DB CPU at 100%', 'Done',
   'Quarterly finance export ran during business hours with heavy GROUP BY scans on orders, saturating DB CPU. Moved it to 02:00 UTC.');

GRANT CONNECT ON DATABASE opsdb TO overwatch_ro;
GRANT USAGE ON SCHEMA public TO overwatch_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO overwatch_ro;
