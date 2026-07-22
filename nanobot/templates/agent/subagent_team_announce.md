[Subagent '{{ label }}' {{ status_text }}]

Task: {{ task }}

Result:
{{ result }}

[Expert-team internal delivery]
This result is internal evidence for the Team Lead. Do not summarize it as a
standalone user-facing answer, do not ask the user whether to retry or continue,
and do not poll the task id with write_stdin. The spawn result is delivered
automatically. Continue the canonical team workflow until synthesis, report
audit, and final artifact delivery are complete.
