---
title: "Runbook: search-api latency regressions"
tags: [search, search-api, latency, p95, orm, n_plus_one, database, pool]
---

## Symptoms

p95 latency several times baseline with `error_rate` essentially flat. Slow-query warnings, and a
connection pool reporting saturation and waiters. Requests complete successfully — they are
merely slow.

## Diagnosis

A latency regression with a flat error rate points at work per request, not failure per request.

1. Compare `rows_fetched` in the slow-query lines against `search_result_limit`. When they track
   each other, each result row is costing a separate query — the N+1 pattern.
2. Check pool waiters. Saturation here is a symptom of query volume, not an undersized pool.
3. Check the change log for a recent deploy touching data access or ORM relationships.

## Mitigation

Roll back the deploy that introduced the regression.

Raising `db_pool_size` lets more N+1 queries run concurrently. It moves the bottleneck to the
database and can make matters worse under load.

Lowering `search_result_limit` reduces the query count by degrading the product, hiding a bug
behind a worse user experience.

Scaling replicas adds capacity for the same inefficient work; p95 per request is unchanged
because the cost is per request, not per host.

## Severity guidance

Degradation is not an outage. A latency regression with no material error rate is a SEV2 even
when the multiple is large. Reserve SEV1 for customer-visible failure.
