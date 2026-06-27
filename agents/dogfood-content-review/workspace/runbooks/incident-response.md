# Incident Response
> What to do when a bobi manager or agent is misbehaving.

## Trigger

- Manager is unresponsive to Slack messages
- Agent is stuck or producing incorrect output
- Event server is down or not routing events

## Steps

1. **Check manager status**
   ```bash
   cd ~/dev/<repo>
   bobi agent <name> status
   ```
   Expected: "Manager: running (session ...)"

2. **Check event server**
   ```bash
   bobi agent <name> event-server status
   ```
   Expected: "Event server: running on port 8080"

3. **Review recent events**
   ```bash
   bobi agent <name> events
   ```
   Look for missing events or delivery failures.

4. **Check manager log**
   ```bash
   tail -n 200 "$BOBI_HOME/agents/<name>/run/state/manager.log"
   ```
   Look for errors, stuck decision loops, or unexpected behavior.

5. **Restart if needed**
   ```bash
   bobi agent <name> restart
   ```
   This preserves the session — the manager resumes where it left off.

6. **Force restart (last resort)**
   ```bash
   bobi agent <name> stop --force
   bobi agent <name> start
   ```
   This kills the process and starts fresh.

## Escalation

If the manager is consistently making bad decisions or agents are
producing incorrect output, file an issue in the bobi repo
with the manager log attached.

Last verified: 2026-06-04
