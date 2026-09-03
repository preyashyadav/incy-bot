---
title: "Policy: change correlation and safe remediation"
tags: [policy, change, deploy, rollback, feature_flag, config, remediation, blast_radius]
---

## Correlating changes with incidents

Most incidents are caused by a change. Read the change log from the incident's onset backwards,
and consider deploys, feature flags, and configuration edits equally — config and flag changes
cause outages as often as deploys and are far more easily overlooked because they leave no
release note.

Beware two failure modes:

- **Recency bias.** The most recent change is not automatically the cause. A config edit six
  hours ago can sit dormant until a flag rollout reaches the affected region.
- **Coincidence.** A deploy shortly before an unrelated traffic spike is not a cause. Require a
  mechanism connecting the change to the symptom, not merely adjacency in time.

Where two changes interact, the fix is usually to reverse the one that is cheapest to reverse and
least deliberate — a hurried config tweak over a planned progressive rollout.

## Choosing a remediation

Prefer, in order:

1. **Reverse the specific change** that introduced the fault — a config value, a flag in one
   region. Smallest blast radius, immediate effect, trivially reversible.
2. **Roll back the deploy**, when the defect is in the artifact and no configuration corrects it.
3. **Restart**, when the fault is accumulated state such as a leak. Always a mitigation; always
   requires a follow-up.
4. **Scale**, only when the fault is genuinely capacity. Scaling does not fix a defect, and
   scaling during an unrelated fault wastes the window in which the real fix could have landed.

Never take an action whose blast radius exceeds the incident's. Prefer region-scoped changes to
global ones, and reversible actions to irreversible ones.

## When not to act

Taking no action is a legitimate, recordable decision. If every metric is inside SLO, say so,
explain why the page fired, and change the alert rather than the system.
