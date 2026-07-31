// Fixture data for the Bobi control-plane kit. Content mirrors the product's
// real vocabulary (sources, agents, workflows, gates) so screens read true.
window.KIT = {
  nav: [
    { id: "queue", label: "queue", icon: "queue", count: 6 },
    { id: "gates", label: "approvals", icon: "human", count: 2, countAccent: true },
    { id: "agents", label: "agents", icon: "parallel" },
    { id: "config", label: "config", icon: "workflow" },
  ],
  events: [
    { id: "e1", icon: "ticket", title: "Export fails on large CSV", source: "linear · ENG-142", agent: "engineer", workflow: "sdlc.yaml", state: "live", time: "2m ago" },
    { id: "e2", icon: "mail", title: "Acme: export failing", source: "email · support@", agent: "support", workflow: "support-triage.yaml", state: "waiting", time: "14m ago", gate: {
      title: "Send the drafted reply to Acme?",
      step: "await: human approval",
      detail: "Classified P1, so the reply waits for a person before it sends.",
      body: [["k","reply:"],["v"," Hi Dana — we reproduced the CSV export"],["v","failure on files over 50MB and opened ENG-311."],["c","# tone: direct, no ETA promised"]],
    } },
    { id: "e3", icon: "bell", title: "P2 · api latency above SLO", source: "pagerduty", agent: "oncall", workflow: "sre-triage.yaml", state: "waiting", time: "26m ago", gate: {
      title: "Apply the proposed rollback to api-gateway?",
      step: "limit: spend · irreversible",
      detail: "Rollback touches production traffic, so it is gated regardless of confidence.",
      body: [["k","action:"],["v"," rollback api-gateway → v2.14.1"],["k","blast_radius:"],["v"," 100% of api traffic"],["a","gate: human approval required"]],
    } },
    { id: "e4", icon: "chat", title: "#eng · quick bug fix?", source: "slack", agent: "engineer", workflow: "sdlc.yaml", state: "live", time: "31m ago" },
    { id: "e5", icon: "ticket", title: "Add CSV size guard", source: "linear · ENG-311", agent: "engineer", workflow: "sdlc.yaml", state: "done", time: "1h ago" },
    { id: "e6", icon: "clock", title: "morning-review", source: "monitor · cron 07:00", agent: "director", workflow: "daily.review", state: "done", time: "3h ago" },
  ],
  agents: [
    { name: "director", role: "triages every event, dispatches the team", icon: "queue", model: "claude-sonnet-4.6", sessions: 41, state: "live" },
    { name: "engineer", role: "ships production-quality code through the SDLC", icon: "code", model: "claude-sonnet-4.6", sessions: 18, state: "live" },
    { name: "support", role: "investigates tickets, drafts replies for review", icon: "headset", model: "claude-haiku-4.5", sessions: 12, state: "waiting" },
    { name: "oncall", role: "root-causes alerts, triages fixes to engineering", icon: "pulse", model: "claude-sonnet-4.6", sessions: 6, state: "idle" },
  ],
  files: [
    { label: "agent.yaml" },
    { label: "roles/engineer/ROLE.md", badge: "generated" },
    { label: "roles/support/ROLE.md", badge: "generated" },
    { label: "workflows/support-triage.yaml", badge: "enforced", badgeAccent: true },
    { label: "monitors/monitors.yaml" },
    { label: "tools/github.md" },
    { label: ".env", badge: "secrets" },
  ],
  panes: {
    "agent.yaml": { tag: "Runtime", lines: [["k","entry_point:"],["v"," director"],["br"],["k","chat:"],["v"," slack"],["br"],["k","runtime:"],["v"," claude-code"],["c","   # your existing plan"]] },
    "roles/engineer/ROLE.md": { tag: "Generated", accent: true, lines: [["a","# Engineer Agent"],["br"],["v","You are a staff engineer who ships"],["br"],["v","production-quality code."],["br"],["br"],["k","## Philosophy"],["br"],["v","1. Read before you write."],["br"],["v","2. Leave it better than found."]] },
    "roles/support/ROLE.md": { tag: "Generated", accent: true, lines: [["a","# Support Agent"],["br"],["v","You investigate before you reply."],["br"],["br"],["k","## Escalate when"],["br"],["v","- the fix needs a code change"],["br"],["v","- the customer asks for a refund"]] },
    "workflows/support-triage.yaml": { tag: "Enforced", accent: true, lines: [["k","steps:"],["br"],["k","  - run:"],["v"," classify_ticket"],["br"],["k","  - run:"],["v"," draft_reply"],["gate","  - await: human approval","  # if P1"],["k","  - run:"],["v"," send_reply"],["br"],["k","  - done:"],["v"," ticket_resolved"]] },
    "monitors/monitors.yaml": { tag: "Watching", lines: [["k","monitors:"],["br"],["k","  - name:"],["v"," morning-review"],["br"],["k","    interval:"],["v"," cron 07:00"],["br"],["k","  - name:"],["v"," stale-pr-check"],["br"],["k","    interval:"],["v"," 1h"],["c","   # nudge after 48h"]] },
    "tools/github.md": { tag: "CLI · MCP · Skill", lines: [["a","# GitHub"],["br"],["v","Interact with repos, PRs, and issues"],["br"],["v","via the gh CLI."],["br"],["br"],["k","## Create a pull request"],["br"],["c","$ "],["v","gh pr create --base main ..."]] },
    ".env": { tag: "Secrets", lines: [["k","SLACK_BOT_TOKEN="],["c","••••••••••••"],["br"],["k","LINEAR_API_KEY="],["c","••••••••••••"],["br"],["k","GITHUB_TOKEN="],["c","••••••••••••"]] },
  },
};
