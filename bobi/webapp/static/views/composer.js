/* The run slab's composer: the one part of it that is pure decision (#987).

   A module rather than a closure in agent.js, for the same reason markdown.js
   is one: which branch a row gets, and what that branch posts, IS the
   contract, and it is only testable while it is importable.
   `tests/test_webapp_composer.py` runs it under Node and reads back what it
   returns.

   Nothing here touches the DOM or the network. */

/** Which composer a row gets. Three outcomes, decided by data rather than by
    the row's kind:

      live    someone is behind this session right now, so the text is
              delivered to it.
      gate    the run is parked on a human approval step. There is no process
              to talk to, and there does not need to be: the answer the gate
              is waiting for is a verdict, and answering it resumes the run
              into the branch that verdict chose.
      ended   the session is over and there is no gate. Nothing can receive a
              message, so the composer offers no way to send one.

    `detail.live` decides the first. A gate needs BOTH the awaiting status and
    a run id: the verdict is delivered by resuming that run, so a row without
    one has nothing to answer. */
export function composerMode(row) {
  const detail = (row && row.detail) || {};
  if (detail.live) return "live";
  if (row && row.status === "awaiting_action" && row.run_id) return "gate";
  return "ended";
}

/** What the gate branch POSTs to the resume route.

    The verdict is the answer; the reply is the human's own words, carried
    beside it so the workflow can put them in front of the agent that has to
    act on them. Both reach the run as its `event` scope.

    The verdict is never inferred from the text: an operator who types
    "looks fine to me" and clicks Reject has rejected it. */
export function resumeBody(verdict, text) {
  return { verdict, reply: (text || "").trim() };
}
