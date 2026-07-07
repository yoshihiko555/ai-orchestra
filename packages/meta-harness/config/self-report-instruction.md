# Self-Report Instruction (meta-harness)

This instruction is injected into every meta-harness scenario run via
`--append-system-prompt-file` (see `docs/design/meta-harness-detailed.md` Sec3-1).
It is not part of the task prompt itself; it only adds a mandatory reporting
step at the very end of your response.

## What you must do

Immediately before you finish your final response (after completing or
abandoning the task), append **exactly one** self-report block in the
following format:

```
[skill-self-report]
{"ambiguities": <integer>, "discretion_fills": <integer>, "retries": <integer>}
[/skill-self-report]
```

- The block must contain a single valid JSON object with exactly the three
  integer fields shown above (no additional fields, no comments).
- Do not wrap the JSON in a code fence. Emit the tags and JSON as plain text.
- Emit the block only once, in your last assistant message.

## Field definitions

- `ambiguities`: the number of times the task prompt was unclear or
  underspecified and you had to interpret it yourself.
- `discretion_fills`: the number of times you had to make a design or
  implementation decision that the prompt did not specify (e.g. choosing a
  file name, format, or approach among multiple reasonable options).
- `retries`: the number of times you had to retry a failed action (a command
  that errored, a test that failed, an edit that had to be redone, etc.).

## Why this matters

Report honestly. This self-report is used to compute a quality score for
this run. Omitting the block, or emitting a block that cannot be parsed,
is treated as the worst-case penalty for this scoring component — it is
never scored more favorably than an honest report. There is no incentive to
suppress this report.
