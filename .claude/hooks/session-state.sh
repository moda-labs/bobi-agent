#!/bin/bash
# Write Claude Code hook events to the manager activity log.
# Receives JSON on stdin with session_id, hook_event_name, etc.
python3 -c "
import sys, json, time
data = json.load(sys.stdin)
entry = {
    'event': data['hook_event_name'],
    'ts': time.time(),
    'session_id': data.get('session_id', ''),
}
import os
log_dir = os.path.expanduser('~/.modastack/manager')
os.makedirs(log_dir, exist_ok=True)
with open(os.path.join(log_dir, 'activity.jsonl'), 'a') as f:
    f.write(json.dumps(entry) + '\n')
"
