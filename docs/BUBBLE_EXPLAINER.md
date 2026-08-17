# Bobi bubble explained

A **bubble** is Bobi's security boundary for one agent instance. It groups
deployments, channel credentials, permissions, and event subscriptions so they
cannot be used by another agent instance.

It consists of:

```text
bubble_id   -> public identifier
bubble_key  -> secret used to sign requests
```

The credentials are persisted in each running agent instance at
`run/state/bubble.json`. The local event server keeps its recognized bubbles in
memory.

```mermaid
flowchart LR
    subgraph Agent["One running Bobi agent instance"]
        BS["run/state/bubble.json<br/>{ bubble_id, bubble_key }"]
        Ensure["bobi/events/server.py<br/>ensure_bubble(event_server_url, project_path)"]
        Login["bobi/auth_bootstrap.py<br/>_wait_for_code(...)"]
        Runtime["sessions, workers and reply channels<br/>register(...)"]
    end

    subgraph EventServer["Event server"]
        Registry["In-memory bubble registry<br/>bubble_id -> bubble_key"]
        Channel["Slack/Discord/WhatsApp<br/>credentials + grants"]
        Deployments["Deployments and<br/>event subscriptions"]
    end

    BS --> Ensure
    Ensure --> Login
    Ensure --> Runtime

    Login -->|"signed registration"| Registry
    Runtime -->|"signed JOIN"| Registry

    Registry --> Channel
    Registry --> Deployments
```

## Normal first boot

```mermaid
sequenceDiagram
    participant B as bobi/events/server.py
    participant F as run/state/bubble.json
    participant E as Event server memory

    B->>F: load_bubble_state(project_path)
    F-->>B: No bubble exists
    B->>E: ensure_bubble() -> unsigned POST /deployments
    E-->>B: bubble_id + bubble_key
    B->>F: save_bubble_state(id, key), mode 0600
    B->>E: register(..., bubble_id, bubble_key)
    E-->>B: Accepted
```

## MOD-307 failure

The event server restarts and forgets its in-memory bubbles, but `bubble.json`
survives:

```mermaid
sequenceDiagram
    participant F as run/state/bubble.json
    participant B as bobi/auth_bootstrap.py
    participant E as Restarted event server memory

    F-->>B: Old bubble A
    Note right of E: Bubble registry is empty
    B->>E: _register_login_channel(..., bubble A)
    E-->>B: 403 BubbleRejected
    Note over B: Old _wait_for_code() stopped here
    Note over B: The OAuth URL may exist,<br/>but no listener remains for the response
```

## MOD-307 recovery

```mermaid
sequenceDiagram
    participant F as run/state/bubble.json
    participant B as bobi/auth_bootstrap.py
    participant S as bobi/events/server.py
    participant E as Restarted event server memory

    F-->>B: Old bubble A
    B->>E: _register_login_channel(..., bubble A)
    E-->>B: 403 BubbleRejected

    B->>S: ensure_bubble(..., force_remint_of="A")
    S->>F: Confirm file still contains A (CAS guard)
    S->>E: unsigned POST /deployments (mint)
    E-->>S: New bubble B credentials
    S->>F: save_bubble_state(B)
    S-->>B: Return bubble B

    B->>E: _register_login_channel(..., bubble B)
    E-->>B: Accepted
    B->>E: register("login-bootstrap", topics, bubble B)
    E-->>B: Accepted
    Note over B: EventServerClient now listens<br/>for the OAuth response
```

The "replace only if still A" operation is a compare-and-swap guard. If two
processes recover simultaneously, the second process sees that another process
already created bubble B and reuses it instead of creating bubble C.

## Multiple Bobi agents

Each **running named-agent instance** normally has its own runtime directory and
therefore its own `state/bubble.json`. Sessions, workers, sub-agents, and reply
channels inside that instance share its bubble.

```mermaid
flowchart TB
    ES["Shared event server<br/>in-memory bubble registry"]

    subgraph A["Agent instance A"]
        AF["agents/agent-a/run/state/bubble.json<br/>{ bubble_id: bub_A, bubble_key: ... }"]
        AE["bobi/events/server.py<br/>ensure_bubble(...)"]
        AM["manager -> register(..., bub_A)"]
        AW["worker -> register(..., bub_A)"]
        AF --> AE
        AE --> AM
        AE --> AW
    end

    subgraph B["Agent instance B"]
        BF["agents/agent-b/run/state/bubble.json<br/>{ bubble_id: bub_B, bubble_key: ... }"]
        BE["bobi/events/server.py<br/>ensure_bubble(...)"]
        BM["manager -> register(..., bub_B)"]
        BW["worker -> register(..., bub_B)"]
        BF --> BE
        BE --> BM
        BE --> BW
    end

    AM --> ES
    AW --> ES
    BM --> ES
    BW --> ES
```

Although both managers subscribe to `inbox/manager`, the event server scopes
non-global topics by bubble:

```mermaid
flowchart LR
    PA["publish_event(project_A, 'inbox/manager', payload)<br/>signed with bub_A"]
    Route["event server routing"]
    TA["bub_A:inbox/manager<br/>delivered to agent A"]
    TB["bub_B:inbox/manager<br/>not delivered"]

    PA --> Route
    Route --> TA
    Route -. "isolated by bubble" .-> TB
```

## Separate state versus shared state

```mermaid
flowchart TB
    subgraph Separate["Normal: separate runtime state"]
        P1["container/process A"] --> F1["agent-a/run/state/bubble.json"]
        F1 --> B1["bubble A"]
        P2["container/process B"] --> F2["agent-b/run/state/bubble.json"]
        F2 --> B2["bubble B"]
    end

    subgraph Shared["Same runtime directory or shared volume"]
        P3["process 1"] --> FS["shared run/state/bubble.json"]
        P4["process 2"] --> FS
        FS --> BS["one shared bubble"]
        Lock["ensure_bubble() lock<br/>prevents two simultaneous mints"] --> FS
    end
```

- Separate runtime state produces separate security boundaries.
- Processes sharing the same runtime state converge on one bubble and are
  treated as parts of the same security instance.
- Sharing an event server is safe: different bubbles isolate identically named
  non-global topics.
- Sharing a runtime directory is not the same as running isolated agents and
  can introduce manager or state-file contention.
