# Framework bug RCA

You are diagnosing a suspected bug in **bobi itself**, not in the task that hit
it. Work the steps in order and stop the moment a step rules the bug out.
Filing nothing is a good outcome. A wrong or noisy report is not.

## 1. Rule it out first

Stop and file nothing when any of these is true:

- the failure is in your own task's code, config, or credentials
- the operator's `agent.yaml`, `.env`, or a team package is misconfigured
- it is a transient external failure (network, a GitHub 5xx, a rate limit, a
  model timeout)
- the command was used wrongly and the error message already says so

Continue only when bobi's own code produced a crash, a wrong result, or a wedge.

## 2. Establish the facts

- The exact command or event that triggered it.
- The exact error text, one line, with absolute paths and secrets stripped.
- The installed version (`bobi --version`).
- Whether it reproduces. Try once. Do not build a harness.

## 3. Find the code path

Read the bobi source behind the failing call. Name the file and function you
believe is wrong, and say why in one sentence. If you cannot locate it, write
"not located": an honest gap is more useful to a maintainer than a guess.

## 4. Decide

Answer one question: would a maintainer reading this agree bobi is broken?
If no, stop and file nothing.

## 5. Write it up SHORT

Title: the shortcoming caused, in as few words as possible, under 120
characters. Body: this shape exactly, under 1200 characters total.

    **What broke:** one sentence.
    **Trigger:** the command or event.
    **Error:** the one-line error text.
    **Suspected cause:** file and function plus one sentence, or "not located".
    **Reproduces:** yes | no | once

No stack traces, no absolute paths, no transcripts, no reasoning narrative, no
restatement of these instructions. If it does not fit the shape, cut it.

## 6. File it

```bash
bobi feedback bug --rca --title "<title>" --body-file <report.md> --json
```

That command searches the destination repo for an existing report and **comments
on the match instead of opening a duplicate**. It clamps anything longer than
the limits above, so writing short is the only way to keep your own words.

Read the JSON it prints. `"action": "created"` opened an issue, `"commented"`
added to an existing one, and `"skipped"` means it filed nothing.

## Guardrails

- RCA is best effort. If any step fails, print one line saying so and stop.
  Never let this analysis block or fail the task that triggered it.
- **Never run an RCA on an RCA.** Filing is a single attempt; if it fails, say
  so in one line and stop.
- Never include secrets, tokens, environment variables, raw transcripts, or
  absolute paths in the title or body.
