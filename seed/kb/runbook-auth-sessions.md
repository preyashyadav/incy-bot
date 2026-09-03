---
title: "Runbook: auth-api session and token failures"
tags: [auth, auth-api, login, session, token, jwt, ttl, identity]
---

## Symptoms

Users authenticate successfully and are then bounced back to the login screen on their next
request. `TokenExpiredException` appears in volume; a refresh storm follows as clients retry.
Availability drops sharply while latency stays close to normal — the requests are fast, they are
just failing.

## Diagnosis

1. Read `issued_seconds_ago` in the TokenExpiredException lines. A value far below the configured
   session lifetime means tokens are being minted with the wrong expiry, not expiring naturally.
2. Compare against `session_ttl_minutes`. If the config is correct but tokens die in seconds, the
   deployed code is misreading the unit.
3. Check the change log for an auth-api deploy shortly before onset.
4. Look for a refresh-rate multiplier well above 1x, which confirms clients are reacting to
   premature expiry rather than a change in traffic.

## Mitigation

Roll back auth-api to the last known good version.

There is no configuration that corrects a unit-conversion defect. Raising `session_ttl_minutes`
scales the wrong number: if the code reads minutes as seconds, setting 120 buys 120 seconds, not
two hours. It looks like an improvement for exactly as long as it takes anyone to notice.

Scaling replicas is irrelevant — every replica runs the same defective build.

## Verification

Session validation failures should return to baseline immediately after the rollback completes.
The refresh storm decays more slowly as clients back off; a brief tail of elevated request volume
after recovery is expected and is not a second incident.
