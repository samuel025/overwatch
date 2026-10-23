# Fault verification

Each fault injected into the demo shop; which Watcher checks fired and what the data showed.

## report_job: Batch job during business hours (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: orders_p95_latency_ms on orders: value 178.801 > threshold 150 (orders /orders p95 latency (ms), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 15.2 | 453 | 0.000 |
| orders | 15.3 | 179 | 0.000 |
| payments | 15.2 | 250 | 0.000 |

Top log lines (last 90s):

- reports: `nightly revenue report started (scheduled job moved to 09:00 UTC by co` x1

## flash_sale: Real traffic surge (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: gateway_p95_latency_ms on gateway: value 17649.2 > threshold 600 (gateway /checkout p95 latency (ms), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 13.4 | 17649 | 0.043 |
| orders | 13.3 | 12 | 0.000 |
| payments | 10.7 | 193 | 0.000 |

Top log lines (last 90s):

- gateway: `timeout calling payments after 1000ms` x13
- gateway: `timeout calling orders after 1000ms` x2
- gateway: `cannot connect to payments: ReadError` x1

## memory_leak: Memory leak and crash loop (OK (surfaces via report))

- surfaces via: **report** ("Customers report checkout fails for a few seconds, roughly every minute.")
- baseline checks firing before injection: none
- checks fired after injection: gateway_error_rate on gateway: value 0.145833 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 14.9 | 241 | 0.146 |
| orders | 14.1 | 47 | 0.000 |
| payments | 12.5 | 17 | 0.000 |

Top log lines (last 90s):

- gateway: `cannot connect to orders: ConnectError` x149
- gateway: `cannot connect to orders: RemoteProtocolError` x3
- orders: `orders started (pid 1)` x2
- gateway: `cannot connect to orders: ReadError` x1

## double_trouble: Two unrelated failures at once (OK)

- surfaces via: **watcher**
- baseline checks firing before injection: none
- checks fired after injection: orders_p95_latency_ms on orders: value 15652.3 > threshold 150 (orders /orders p95 latency (ms), last 40s), gateway_p95_latency_ms on gateway: value 1034.03 > threshold 600 (gateway /checkout p95 latency (ms), last 40s), gateway_error_rate on gateway: value 0.984589 > threshold 0.05 (gateway /checkout error rate (0-1), last 40s)

| service | rps | p95 ms | error rate |
|---|---|---|---|
| gateway | 14.5 | 1034 | 0.985 |
| orders | 0.9 | 15652 | 0.000 |
| payments | 1.4 | 14 | 0.000 |

Top log lines (last 90s):

- gateway: `timeout calling orders after 1000ms` x630

