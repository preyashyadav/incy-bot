---
title: "Policy: severity classification"
tags: [policy, severity, sev1, sev2, sev3, rubric, classification]
---

## Rubric

**SEV1 — customer-visible failure.** A core flow is broken for a material share of users.
Payments failing, login unavailable, checkout unable to complete. Availability below SLO or an
error rate above a few percent on a critical path. Pages immediately, requires an incident
channel and status communication.

**SEV2 — degradation.** The service works but materially worse. Latency regressions, partial
failures, elevated errors on a non-critical path, or a mitigated issue expected to recur. Pages
during business hours.

**SEV3 — minor or no customer impact.** Alerting defects, internal-only issues, and conditions
already absorbed by automation. Handled on the next working day.

## Applying it

Severity is set by **customer impact**, not by the size of the metric change. A 10x latency
regression with no failed requests is a SEV2; a 3% error rate on checkout is a SEV1. The question
is what a customer cannot do, not how alarming the graph looks.

Severity may be revised in either direction as evidence arrives. Record the revision and the
reason; a severity that quietly changes is indistinguishable from one that was wrong.

A page that fires while every metric is inside SLO is not an incident. Classify it SEV3, state
that no action was taken, and raise the threshold as a follow-up.
