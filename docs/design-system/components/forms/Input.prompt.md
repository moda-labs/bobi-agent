A Bobi text field — mono, hairline border, violet focus.

```jsx
<Input label="Agent name" placeholder="my-agent" hint="lowercase, no spaces" />
<Input label="Command" prefix="$" defaultValue="bobi agent my-agent start" />
<Input label="Cron" defaultValue="cron 07:00" invalid hint="unrecognized schedule" />
```
