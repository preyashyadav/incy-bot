---
title: "Runbook: traffic surges and load-proportional alerts"
tags: [traffic, surge, autoscaler, capacity, load, noisy_neighbor, false_positive, feed-api]
---

## Symptoms

Elevated latency and CPU alongside a large rise in `request_rate_rps`. Error rate normal.
Autoscaler events shortly before the page.

## Diagnosis

Ask first whether anything is actually wrong.

1. Compare current `request_rate_rps` against baseline. A large multiple with proportional
   latency is capacity behaviour, not a fault.
2. Check replica count. If the autoscaler has already added capacity, per-replica utilisation may
   be at or near normal despite the traffic.
3. Compare every metric against its SLO rather than against its baseline. Elevated is not the
   same as breaching.
4. Check for a scheduled cause — a marketing send, a partner integration going live, a regional
   failover.

## Mitigation

Usually none. If all metrics are inside SLO and the autoscaler has responded, the correct action
is to take no action and say why.

Do not scale down to "return to normal" replica counts during a surge. The replica count *is* the
response to the surge; removing it converts a non-incident into a real one.

Do not roll back a deploy that correlates only by proximity in time. A deploy hours before an
unrelated traffic spike is a coincidence, and a needless rollback during elevated load adds risk
for no benefit.

## Follow-up

A page that fires on absolute latency during a proportional surge is a threshold problem. Raise
it as an alerting defect: the threshold should be load-aware, or expressed as a burn rate against
an SLO rather than a fixed millisecond value.
