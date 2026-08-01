# Bobi — Control plane UI kit

An interactive recreation of the Bobi product surface, composed **only** from the
published components in `../../components/`. It is a fidelity reference, not
production code: state is local, data is fixtures in `kit-data.js`.

Open `index.html` and click through:

- **Queue** — the event list. Every Bobi run starts as an event, so this is the
  product's home. Clicking a row whose state is `gate` jumps to Approvals.
- **Approvals** — runs stopped at a human gate, each showing the payload under
  review. Approve/Reject resolves the gate and decrements the sidebar count.
- **Agents** — the director plus the team, each with model and session count,
  followed by the nightly memory-consolidation figure.
- **Config** — the agent's real files. Select a file in the tree to swap the
  config pane; toggle monitors; read the terminal.

## Files

| File | What it is |
|---|---|
| `index.html` | App shell + view routing |
| `Chrome.jsx` | `KitSideNav`, `TopBar`, `KitPane` |
| `Screens.jsx` | `QueueScreen`, `GatesScreen`, `AgentsScreen`, `ConfigScreen` |
| `kit-data.js` | Fixture events, agents, files, config panes |
| `component-shim.jsx` | **Generated.** The design-system components with ESM keywords stripped, each wrapped in an IIFE, so the kit runs with no bundler. Regenerate it if you change a component; delete it when consuming the system from a real app. |

## What this kit is asserting about the product

The marketing site describes a CLI framework, so no product UI existed to
recreate. These screens are a **proposal** built strictly from the language the
source already established — the loop (signal → triage → dispatch → report),
gates as workflow nodes, an agent being its files, the paper/void relationship,
and the fixed status palette. Anything that needed inventing is called out in
the root `readme.md` under "Intentional additions"; treat those as open for
review rather than settled.
