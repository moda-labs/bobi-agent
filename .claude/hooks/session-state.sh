#!/bin/bash
# Write Claude Code hook events to the manager activity log.
# On Stop: also relay the assistant response to Slack via the relay config.
python3 -c "
import sys, json, time, os

data = json.load(sys.stdin)
entry = {
    'event': data['hook_event_name'],
    'ts': time.time(),
    'session_id': data.get('session_id', ''),
}

# Write activity log
log_dir = os.path.expanduser('~/.modastack/manager')
os.makedirs(log_dir, exist_ok=True)
with open(os.path.join(log_dir, 'activity.jsonl'), 'a') as f:
    f.write(json.dumps(entry) + '\n')

# On Stop: relay assistant response to Slack
if data['hook_event_name'] == 'Stop':
    msg = data.get('last_assistant_message', '')
    if msg:
        try:
            import yaml
            config_path = os.path.expanduser('~/.modastack/config.yaml')
            with open(config_path) as cf:
                config = yaml.safe_load(cf) or {}
            slack = config.get('slack', {})
            token = slack.get('bot_token', '')
            channel = slack.get('dm_channel', '') or 'D0B51JP1N4C'
            if token:
                import urllib.request
                text = msg[:3000] + '\n_(truncated)_' if len(msg) > 3000 else msg
                payload = json.dumps({'channel': channel, 'text': text}).encode()
                req = urllib.request.Request(
                    'https://slack.com/api/chat.postMessage',
                    data=payload,
                    headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                )
                urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
"
