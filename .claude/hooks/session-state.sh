#!/bin/bash
# Write Claude Code hook events to the manager activity log.
# Receives JSON on stdin with session_id, hook_event_name, last_assistant_message, etc.
python3 -c "
import sys, json, time, os
data = json.load(sys.stdin)
entry = {
    'event': data['hook_event_name'],
    'ts': time.time(),
    'session_id': data.get('session_id', ''),
}
if data['hook_event_name'] == 'Stop':
    msg = data.get('last_assistant_message', '')
    if msg:
        entry['response'] = msg
log_dir = os.path.expanduser('~/.modastack/manager')
os.makedirs(log_dir, exist_ok=True)
with open(os.path.join(log_dir, 'activity.jsonl'), 'a') as f:
    f.write(json.dumps(entry) + '\n')
"
