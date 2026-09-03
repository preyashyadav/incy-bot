---
title: "Template: incident communication"
tags: [comms, template, status_update, communication, postmortem, stakeholder]
---

## Initial update

Send within minutes of declaring. State what is happening, who is affected, what is being done,
and when the next update will come.

Avoid asserting a root cause before it is established. Use "under investigation" and "appears
related to". An initial update that names the wrong cause is far more expensive than one that
names none, because it anchors everyone who reads it.

## Mitigation update

State what changed, the current status, what risk remains, and the next step. If the action taken
was a mitigation rather than a fix — a restart against a leak, a flag disabled without addressing
the underlying defect — say so plainly. A thread that reads "resolved" after a restart teaches the
next responder the wrong lesson.

## Resolution update

Confirm recovery against the specific metrics that breached, note anything still outstanding, and
link the follow-up work.

## Tone

Concrete and unhedged. Give numbers, name the metric, name the action. Confidence should be
stated explicitly ("high confidence", "still unconfirmed") rather than implied through vagueness.
