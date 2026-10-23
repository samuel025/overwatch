# Fault verification

Each fault injected into the demo shop; which Watcher checks fired and what the data showed.

## slow_query: Migration dropped an index (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: orders_p95_latency_ms on orders: value 15160.4 > threshold 150 (orders /orders p95 latency (ms), last 40s), gateway_p95_latency_ms on gateway: value 1031 > threshold 600 (gateway /checkout p95 latency (ms), last 40s), gateway_error_rate on gateway: value 0.958929 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s), database_cpu_pct on postgres: value 100.525 > threshold 85 (database CPU as % of its limit, last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 14.5 | 1031 | 0.959 |
| orders | 1.3 | 15160 | 0.000 |
| payments | 1.3 | 80 | 0.000 |

Top log lines (last 90s):

- gateway: `upstream request timeout: orders (1000ms)` x662
- gateway: `upstream orders returned 500` x4
- orders: `GET /orders 500: invalid JSON in request body` x4
- gateway: `GET /checkout 500: upstream connection reset by peer, retried` x2

## bad_deploy: Buggy release (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: payments_error_rate on payments: value 0.363852 > threshold 0.05 (payments /pay error rate (0-1) across instances, last 40s), gateway_error_rate on gateway: value 0.365804 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 14.5 | 132 | 0.366 |
| orders | 14.5 | 7 | 0.003 |
| payments | 7.2 | 73 | 0.364 |

Top log lines (last 90s):

- payments: `POST /pay 500: unhandled exception in /pay: ValueError('unexpected tok` x249
- gateway: `upstream payments-b returned 500` x128
- gateway: `upstream payments-a returned 500` x125
- gateway: `GET /checkout 500: client disconnected before response was sent` x5

## pool_exhaustion: Connection pool too small (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: gateway_p95_latency_ms on gateway: value 1155.22 > threshold 600 (gateway /checkout p95 latency (ms), last 40s), payments_error_rate on payments: value 0.345059 > threshold 0.05 (payments /pay error rate (0-1) across instances, last 40s), gateway_error_rate on gateway: value 0.941667 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 15.9 | 1155 | 0.942 |
| orders | 16.3 | 7 | 0.002 |
| payments | 7.7 | 2426 | 0.345 |

Top log lines (last 90s):

- gateway: `upstream request timeout: payments-a (1000ms)` x366
- gateway: `upstream request timeout: payments-b (1000ms)` x338
- payments: `PoolTimeout: couldn't get a connection after 2.00 sec` x265
- gateway: `upstream orders returned 500` x4

## downstream_timeout: Slow dependency (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: gateway_p95_latency_ms on gateway: value 1162.73 > threshold 600 (gateway /checkout p95 latency (ms), last 40s), gateway_error_rate on gateway: value 0.926101 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 15.5 | 1163 | 0.926 |
| orders | 15.8 | 6 | 0.005 |
| payments | 7.6 | 1504 | 0.007 |

Top log lines (last 90s):

- gateway: `upstream request timeout: payments-a (1000ms)` x342
- gateway: `upstream request timeout: payments-b (1000ms)` x327
- gateway: `upstream orders returned 500` x7
- orders: `GET /orders 500: client disconnected before response was sent` x4

## red_herring: Innocent deploy at the wrong time (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: gateway_p95_latency_ms on gateway: value 1146.26 > threshold 600 (gateway /checkout p95 latency (ms), last 40s), gateway_error_rate on gateway: value 0.976449 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 13.9 | 1146 | 0.976 |
| orders | 14.3 | 6 | 0.002 |
| payments | 6.9 | 1505 | 0.002 |

Top log lines (last 90s):

- gateway: `upstream request timeout: payments-a (1000ms)` x312
- gateway: `upstream request timeout: payments-b (1000ms)` x307
- gateway: `upstream orders returned 500` x3
- gateway: `GET /checkout 500: request body read timed out after 30s` x2

## lock_contention: Long transaction holding a lock (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: gateway_p95_latency_ms on gateway: value 1106.77 > threshold 600 (gateway /checkout p95 latency (ms), last 40s), payments_error_rate on payments: value 0.958491 > threshold 0.05 (payments /pay error rate (0-1) across instances, last 40s), gateway_error_rate on gateway: value 0.96875 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 13.7 | 1107 | 0.969 |
| orders | 14.0 | 6 | 0.009 |
| payments | 6.4 | 2076 | 0.958 |

Top log lines (last 90s):

- payments: `PoolTimeout: couldn't get a connection after 2.00 sec` x578
- gateway: `upstream request timeout: payments-a (1000ms)` x311
- gateway: `upstream request timeout: payments-b (1000ms)` x299
- gateway: `upstream orders returned 500` x7

## config_timeout: Overly aggressive timeout (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: gateway_error_rate on gateway: value 0.81062 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 15.3 | 83 | 0.811 |
| orders | 10.3 | 6 | 0.005 |
| payments | 1.4 | 74 | 0.000 |

Top log lines (last 90s):

- gateway: `upstream request timeout: orders (4ms)` x574
- gateway: `upstream orders returned 500` x5
- gateway: `GET /checkout 500: request body read timed out after 30s` x2
- gateway: `GET /checkout 500: client disconnected before response was sent` x2

## retry_storm: Retries amplify a failure (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: payments_error_rate on payments: value 0.482215 > threshold 0.05 (payments /pay error rate (0-1) across instances, last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 14.3 | 433 | 0.035 |
| orders | 14.2 | 7 | 0.002 |
| payments | 13.2 | 82 | 0.482 |

Top log lines (last 90s):

- payments: `POST /pay 500: unhandled exception in /pay: ValueError('unexpected tok` x596
- gateway: `upstream payments-b returned 500` x12
- gateway: `upstream payments-a returned 500` x8
- gateway: `upstream orders returned 500` x3

## n_plus_one: N+1 queries after a release (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: orders_p95_latency_ms on orders: value 7535.89 > threshold 150 (orders /orders p95 latency (ms), last 40s), gateway_p95_latency_ms on gateway: value 1027.44 > threshold 600 (gateway /checkout p95 latency (ms), last 40s), gateway_error_rate on gateway: value 0.967687 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s), database_cpu_pct on postgres: value 100.767 > threshold 85 (database CPU as % of its limit, last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 14.2 | 1027 | 0.968 |
| orders | 2.2 | 7536 | 0.042 |
| payments | 0.9 | 78 | 0.000 |

Top log lines (last 90s):

- gateway: `upstream request timeout: orders (1000ms)` x617
- gateway: `upstream orders returned 500` x8
- orders: `GET /orders 500: client disconnected before response was sent` x4
- gateway: `upstream payments-a returned 500` x3

## report_job: Batch job during business hours (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: orders_p95_latency_ms on orders: value 172.413 > threshold 150 (orders /orders p95 latency (ms), last 40s), database_cpu_pct on postgres: value 100.425 > threshold 85 (database CPU as % of its limit, last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 14.6 | 458 | 0.009 |
| orders | 14.5 | 172 | 0.003 |
| payments | 7.3 | 260 | 0.004 |

Top log lines (last 90s):

- gateway: `upstream orders returned 500` x3
- gateway: `upstream payments-b returned 500` x3
- gateway: `GET /checkout 500: invalid JSON in request body` x2
- payments: `POST /pay 500: request body read timed out after 30s` x2

## flash_sale: Real traffic surge (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: gateway_p95_latency_ms on gateway: value 5018.31 > threshold 600 (gateway /checkout p95 latency (ms), last 40s), gateway_error_rate on gateway: value 0.953795 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 51.1 | 5018 | 0.954 |
| orders | 5.1 | 93 | 0.003 |
| payments | 2.0 | 183 | 0.000 |

Top log lines (last 90s):

- gateway: `upstream request timeout: orders (1000ms)` x18
- gateway: `upstream request timeout: payments-b (1000ms)` x4
- gateway: `upstream orders returned 500` x3
- gateway: `upstream request timeout: payments-a (1000ms)` x3

## memory_leak: Memory leak and crash loop (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: gateway_error_rate on gateway: value 0.134849 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s), container_restarts on containers: value 2 >= threshold 1 (container restarts in the last 3 minutes (worst instance))

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 14.0 | 208 | 0.135 |
| orders | 13.4 | 11 | 0.008 |
| payments | 6.3 | 78 | 0.000 |

Top log lines (last 90s):

- gateway: `upstream connect error: orders (ConnectError)` x140
- gateway: `upstream orders returned 500` x3
- gateway: `upstream connect error: orders (RemoteProtocolError)` x3
- orders: `GET /orders 500: request body read timed out after 30s` x2

## database_down: Database unreachable (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: orders_p95_latency_ms on orders: check could not run: (psycopg.OperationalError) connection failed: connection to server at "127.0.0.1", port 55432 failed: Connection refused, gateway_p95_latency_ms on gateway: check could not run: (psycopg.OperationalError) connection failed: connection to server at "127.0.0.1", port 55432 failed: Connection refused, payments_error_rate on payments: check could not run: (psycopg.OperationalError) connection failed: connection to server at "127.0.0.1", port 55432 failed: Connection refused, gateway_error_rate on gateway: check could not run: (psycopg.OperationalError) connection failed: connection to server at "127.0.0.1", port 55432 failed: Connection refused, container_restarts on containers: check could not run: (psycopg.OperationalError) connection failed: connection to server at "127.0.0.1", port 55432 failed: Connection refused, database_cpu_pct on postgres: check could not run: (psycopg.OperationalError) connection failed: connection to server at "127.0.0.1", port 55432 failed: Connection refused

| service | rps | p95 ms | error rate |
|---|---|---|---|
| database unreachable | connection failed: connection to server at "127.0.0.1", port |  |  |

## double_trouble: Two unrelated failures at once (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: orders_p95_latency_ms on orders: value 16557.7 > threshold 150 (orders /orders p95 latency (ms), last 40s), gateway_p95_latency_ms on gateway: value 1032.21 > threshold 600 (gateway /checkout p95 latency (ms), last 40s), gateway_error_rate on gateway: value 0.923214 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s), database_cpu_pct on postgres: value 101.067 > threshold 85 (database CPU as % of its limit, last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 15.6 | 1032 | 0.923 |
| orders | 1.3 | 16558 | 0.063 |
| payments | 2.6 | 69 | 0.000 |

Top log lines (last 90s):

- gateway: `upstream request timeout: orders (1000ms)` x699
- gateway: `upstream orders returned 500` x4
- orders: `GET /orders 500: invalid JSON in request body` x2
- orders: `GET /orders 500: client disconnected before response was sent` x1

## bad_replica: One bad instance (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: payments_error_rate on payments: value 0.497093 > threshold 0.05 (payments /pay error rate (0-1) across instances, last 40s), gateway_error_rate on gateway: value 0.529823 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 15.1 | 120 | 0.530 |
| orders | 15.2 | 7 | 0.003 |
| payments | 7.5 | 39 | 0.497 |

Top log lines (last 90s):

- payments: `POST /pay 500: unhandled exception in /pay: ValueError('unexpected tok` x356
- gateway: `upstream payments-b returned 500` x354
- gateway: `GET /checkout 500: upstream connection reset by peer, retried` x3
- gateway: `upstream orders returned 500` x2

## connection_leak: Connection leak (gradual) (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: gateway_p95_latency_ms on gateway: value 678.101 > threshold 600 (gateway /checkout p95 latency (ms), last 40s), payments_error_rate on payments: value 0.49659 > threshold 0.05 (payments /pay error rate (0-1) across instances, last 40s), gateway_error_rate on gateway: value 0.501561 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 15.2 | 678 | 0.502 |
| orders | 15.5 | 7 | 0.003 |
| payments | 7.4 | 1080 | 0.497 |

Top log lines (last 90s):

- payments: `PoolTimeout: couldn't get a connection after 2.00 sec` x287
- gateway: `upstream request timeout: payments-b (1000ms)` x165
- gateway: `upstream request timeout: payments-a (1000ms)` x126
- gateway: `upstream payments-a returned 500` x7

## provider_rate_limit: Third-party rate limit (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: payments_error_rate on payments: value 0.370858 > threshold 0.05 (payments /pay error rate (0-1) across instances, last 40s), gateway_error_rate on gateway: value 0.369547 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 13.5 | 146 | 0.370 |
| orders | 13.4 | 7 | 0.000 |
| payments | 6.6 | 76 | 0.371 |

Top log lines (last 90s):

- payments: `POST https://psp.example.com/v1/charge returned 429 Too Many Requests ` x222
- gateway: `upstream payments-a returned 500` x116
- gateway: `upstream payments-b returned 500` x113
- gateway: `GET /checkout 500: client disconnected before response was sent` x3

## cert_expired: Expired TLS certificate (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: payments_error_rate on payments: value 0.966903 > threshold 0.05 (payments /pay error rate (0-1) across instances, last 40s), gateway_error_rate on gateway: value 0.967262 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 15.5 | 72 | 0.967 |
| orders | 15.5 | 5 | 0.002 |
| payments | 7.8 | 19 | 0.967 |

Top log lines (last 90s):

- payments: `POST https://psp.example.com/v1/charge failed: [SSL: CERTIFICATE_VERIF` x685
- gateway: `upstream payments-a returned 500` x355
- gateway: `upstream payments-b returned 500` x336
- gateway: `GET /checkout 500: invalid JSON in request body` x2

