---
title: "Runbook: checkout-api memory exhaustion"
tags: [checkout, checkout-api, memory, heap, oom, gc, leak, cache, restart]
---

## Symptoms

Memory climbing steadily toward the heap limit, GC pauses beyond threshold, `OutOfMemoryError`
during allocation, and an error rate that rises with uptime rather than with traffic.

## Diagnosis

The distinguishing feature of a leak is its correlation with **uptime**, not load.

1. Compare time since last restart against the onset of degradation. A consistent interval —
   here, roughly four hours — indicates accumulation.
2. Check whether traffic actually rose. If `request_rate_rps` is flat while memory climbs, the
   cause is retention, not demand.
3. Look for an unbounded cache. Entries with a TTL that is never enforced grow without limit.

## Mitigation

Restart the service. The heap clears and the service recovers immediately.

**This is a mitigation, not a fix.** The leak is still present and the clock restarts with it.
Always pair the restart with a follow-up to bound the offending cache, and say so explicitly in
the incident thread — a restart that silently resolves an incident teaches the next responder
that the problem went away.

Scaling replicas spreads traffic across more instances, each of which still leaks. It extends
the interval between failures and makes the pattern harder to recognise.

Rolling back is appropriate only if the leak was introduced by a recent deploy and the previous
version is known clean.

## Verification

Memory returns to baseline and the error rate clears within one window. Expect recurrence after
approximately the same uptime interval. The incident is mitigated, not resolved, until the cache
is bounded.
