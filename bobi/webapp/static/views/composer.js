/* The run slab's composer: the one part of it that is pure text (#987).

   A module rather than a closure in agent.js, for the same reason
   markdown.js is one: this string IS the contract with the team manager, and
   it is only testable while it is importable. `tests/test_webapp_composer.py`
   runs it under Node and reads back what it builds.

   Nothing here touches the DOM or the network. */

/** The message the console relays to the team manager when the operator
    types on a run whose session has ended.

    It is a request, not a spawn: the manager is an agent and it decides. So
    the relay carries everything a fresh session needs to pick the work up -
    which session ended, which run it belonged to, and the operator's own
    words, verbatim and delimited so their prose cannot be read as the
    console's own framing.

    It also names the one thing that must not happen. A suspended run records
    the step AFTER its gate, so resuming one skips the approval it is parked
    on and starts the next step instead. The page cannot resume - there is no
    such call in any view, and a test pins that - and the instruction says so
    out loud as well, because the manager can resume even though the page
    cannot. */
export function continuationRelay(row, text) {
  const detail = (row && row.detail) || {};
  const lines = [
    "[bobi console] The operator typed this on a run whose session has ended.",
    "",
  ];
  const field = (label, value) => {
    if (value === "" || value == null) return;
    lines.push(label.padEnd(19) + value);
  };
  field("target_session:", row.session_id);
  field("workflow:", row.kind === "workflow" ? row.title : "");
  field("run_key:", detail.run_key);
  field("run_id:", row.run_id);
  field("awaiting:", detail.await_event);
  // -1 means "no step", and 0 is a real one, so this cannot test truthiness.
  field("suspended_at_step:",
        detail.suspended_at_step >= 0 ? detail.suspended_at_step : "");

  lines.push("", "--- the operator's message, verbatim ---", text,
             "--- end of message ---", "");

  if (row.run_id) {
    lines.push(
      "Start a FRESH session to carry this forward, seeded with that run's " +
      "durable context: the handoffs under `state/sessions/" +
      (row.session_id || "<session>") + "/` and the run record's variable " +
      "scopes. Do NOT resume run " + row.run_id + " - a suspended run " +
      "records the step AFTER its gate, so resuming it skips the gate " +
      "entirely and starts at the next step.");
  } else {
    lines.push(
      "Start a FRESH session to carry this forward, seeded with whatever " +
      "that session left on disk.");
  }
  return lines.join("\n");
}
