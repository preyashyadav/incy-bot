---
title: "Runbook: payments-api upstream gateway failures"
tags: [payments, payments-api, gateway, timeout, circuit_breaker, enable_new_gateway]
---

## Symptoms

Elevated `error_rate` on payments-api together with a rising `upstream_timeout_rate`, and
`CircuitBreakerOpenException` in the logs. Checkout fails for customers; the health endpoint
reports UNHEALTHY. p95 latency climbs toward the configured gateway timeout because requests sit
waiting before being abandoned.

## Diagnosis

Establish whether the upstream is genuinely slow or whether we stopped waiting long enough for it.

1. Compare `upstream_timeout_rate` against `error_rate`. When they move together, the errors are
   timeouts rather than rejections from the gateway.
2. Read `gateway_timeout_ms`. The new gateway client needs headroom above 1.5s to complete its
   TLS handshake and internal retry budget against the payment processor. Anything below that is
   too aggressive.
3. Check whether `enable_new_gateway` is on in the affected region. The old client tolerates a
   1s timeout; the new one does not.
4. Review the change log for deploys, flag flips, and config edits in the hours before onset.

## The interaction that causes this

Neither a tightened timeout nor the new gateway client breaks payments on its own. The failure
requires both: the new client's handshake exceeds a sub-1.5s budget, every request times out, and
the circuit breaker opens. Two separately reasonable changes, landed hours apart, combine into an
outage — which is why the change log must be read as a whole rather than only its most recent
entry.

## Mitigation

Prefer restoring `gateway_timeout_ms` to 2000. It reverses the change that actually broke
payments, takes effect immediately, and is fully reversible.

Disabling `enable_new_gateway` in the affected region also clears the fault and is the right call
if the timeout value is load-bearing for another reason. It costs more, because it rolls back a
deliberate progressive rollout.

Rolling back the payments-api deploy does **not** help. The deployed artifact is not at fault; it
ships both gateway clients and selects between them on the flag.

Scaling replicas does not help either. Each replica fails the same way, so adding capacity
lowers CPU while leaving the error rate untouched.

## Verification

`error_rate` should return to baseline within one metrics window, `upstream_timeout_rate` to
zero, and the circuit breaker should close. If the error rate persists after the timeout is
restored, the upstream processor itself is degraded and this runbook does not apply — escalate
to the payments vendor.
