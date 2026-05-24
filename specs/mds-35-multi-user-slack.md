# MDS-35: Multi-user Slack conversations — implicit stakeholder tracking and proactive updates

## Problem & Solution

**Problem:** When modastack runs on a remote box and multiple people message it on Slack simultaneously, the manager has no way to:

1. **Maintain separate conversation context** — all Slack messages land in a flat batch every 5 seconds. If Alice asks about the auth migration and Bob asks about a deploy timeline in the same batch, the manager processes them together with no separation. Follow-ups lose continuity.
2. **Know who to notify about ticket updates** — when a ticket moves to In Review or a PR lands, the manager doesn't know who cares. It has no record of who asked about what, who filed what, or where the conversation happened.
3. **Proactively close the loop** — a real EM remembers who's waiting on what and updates them when things change. The manager currently doesn't.

**Who it solves for:** Teams of 2-5 people communicating with modastack on Slack — founders, engineers, and stakeholders who each care about different tickets and expect the bot to remember their context.

**Solution:** Three changes to make the manager behave like an attentive EM who tracks conversations and stakeholders implicitly:

1. **Manager prompt additions** — new sections on per-person context switching, stakeholder tracking in memory, and proactive notification behavior
2. **Memory file structure** — a stakeholder/conversation section in the manager's persistent memory (natural language, not schema)
3. **Event consumer context injection** — enrich event batches with the sender's recent conversation history from memory

## Scope

**In:**
- Manager prompt: multi-user awareness, stakeholder tracking, proactive update, and filtering guidance (~60-80 new lines)
- Consumer: read memory file and inject per-person context alongside Slack events
- Memory: bootstrap stakeholder section structure and pruning guidance in prompt
- Startup prompt: remove hardcoded single-user DM channel, make multi-user aware

**Out:**
- Multiple manager sessions / parallel processing (single session, context-switches)
- Formal conversation data structures / database (stays as natural-language memory)
- Per-user rate limiting or queuing
- Slack channel management (joining/leaving channels)
- User authentication or access control

## Technical Approach

### 1. Manager prompt additions (`manager/prompt.md`)

Add three new sections after the existing "How you work" section:

#### Section: "Multi-user awareness"

Instructs the manager to:
- Identify the sender of each Slack message (the `from` field is already in events)
- Before responding to a person, recall their recent conversation context from memory
- Never confuse one person's context with another's — when processing a batch with messages from multiple people, handle each person's messages separately
- When responding, respond in the same place the person messaged (same channel, same DM, same thread)

#### Section: "Stakeholder tracking"

Instructs the manager to maintain a `## Stakeholders` section in its memory file (`~/.modastack/manager/memory.md`). Key behaviors:

- When someone asks about, files, is assigned to, or mentions a ticket → note the association in memory
- Format: natural language notes, one line per association. Include: who, which ticket, where (channel/DM + thread_ts if applicable), why they care, urgency level
- Example entries:
  ```
  Alice asked about MDS-12 (auth migration) in #engineering — she's blocked waiting for it
  Bob filed MDS-15 (dashboard bug) via DM, seemed urgent to him
  CEO mentioned onboarding flow in #general, keep him posted on MDS-18
  ```
- Prune stale entries: when a ticket is closed/done, remove its stakeholder entries. When entries are >7 days old with no follow-up, drop them.

#### Section: "Proactive updates"

Instructs the manager to check stakeholder memory when ticket state changes and notify the right people:

- **Trigger**: any ticket state change (In Progress → In Review, PR created, PR merged, engineer blocked, ticket closed)
- **Lookup**: check `## Stakeholders` section in memory for anyone associated with the ticket
- **Filtering** — not everyone gets everything:
  - Someone actively blocked → immediate update when the blocker clears
  - The person who filed the ticket → close the loop when it's done
  - Someone who asked once in passing → brief update or nothing
  - A channel mention → update goes back to the channel, not a DM
- **Where to notify**: respond in the same place the original conversation happened (channel_id + thread_ts from the stakeholder entry)
- **Don't spam**: use judgment. One update per state change per person, max. Skip redundant or low-value updates.

#### Modification: Startup prompt (`manager/session.py`)

The current startup prompt hardcodes a single DM channel:
```python
f"Your Slack DM channel with Zach is D0B51JP1N4C. "
```

Change to:
```python
f"Multiple people may message you on Slack. Track who's talking and "
f"maintain conversation context per person in your memory file. "
```

The manager will discover DM channels from the `channel_id` field in Slack events rather than relying on a hardcoded value.

### 2. Memory file structure

The manager prompt already references `~/.modastack/manager/memory.md` (line 100: `update_memory` action) but the file doesn't exist. The prompt tells the manager to "write to" it but never defines structure.

Add to the prompt's memory guidance:

```
Your memory file at ~/.modastack/manager/memory.md has these sections:

## Active conversations
Per-person context — who said what recently, what they're waiting on.
One block per person. Update on every interaction, prune after 24h of silence.

## Stakeholders
Who cares about which tickets, where they asked, and how urgently.
One line per association. Prune when tickets close or entries go stale (>7 days).

## Processed comments
Timestamps of comments you've already acted on (existing behavior).

## Notes
General context, dependencies, and decisions (existing behavior).
```

No code creates this file — the manager creates it on first write via the `update_memory` action or by writing directly. The prompt just tells it what sections to maintain.

### 3. Event consumer context injection (`manager/events/consumer.py`)

Currently `_write_events_file()` writes events as flat markdown. When the batch contains Slack messages, the consumer should enrich the file with per-person context from memory.

**Changes to `_write_events_file()`:**

After writing the event list, append a `## Conversation context` section that includes relevant memory excerpts for each unique sender in the batch:

```python
def _write_events_file(events: list[dict]) -> None:
    # ... existing event writing ...

    # Inject per-person context from memory for Slack events
    slack_senders = set()
    for e in events:
        if e["source"] == "slack" and e["data"].get("from"):
            slack_senders.add(e["data"]["from"])

    if slack_senders:
        memory = _read_memory()
        if memory:
            lines.append("## Conversation context")
            lines.append("")
            for sender in sorted(slack_senders):
                context = _extract_person_context(memory, sender)
                if context:
                    lines.append(f"### {sender}")
                    lines.append(context)
                    lines.append("")
```

**New helper functions:**

```python
MEMORY_PATH = Path.home() / ".modastack" / "manager" / "memory.md"

def _read_memory() -> str:
    """Read the manager's memory file, if it exists."""
    if MEMORY_PATH.exists():
        return MEMORY_PATH.read_text()
    return ""

def _extract_person_context(memory: str, person: str) -> str:
    """Extract lines from memory that mention a specific person.

    Scans the Active conversations and Stakeholders sections for
    lines containing the person's name. Returns a short excerpt
    (max 500 chars) to avoid blowing up context.
    """
    relevant = []
    for line in memory.splitlines():
        if person.lower() in line.lower():
            relevant.append(line.strip())
    if not relevant:
        return ""
    text = "\n".join(relevant)
    return text[:500] + ("..." if len(text) > 500 else "")
```

**Design rationale for simple string matching:** The memory file is natural language written by the manager itself, so person names will appear as written. We don't need fuzzy matching or NLP — if the manager wrote "Alice asked about MDS-12", searching for "Alice" will find it. The 500-char cap prevents memory from overwhelming the event file.

### 4. Slack event enrichment — no changes needed

The Slack socket client (`manager/events/slack_socket.py`) already includes `from`, `from_id`, `channel_id`, `ts`, and `thread_ts` in every event. The consumer's `_write_events_file()` already writes `from` and `channel_id` to the pending events file. No changes needed to the Slack layer.

### Design decisions

**Why natural language memory, not structured data?**
The issue is explicit about this: "No formal schema — the manager writes it like notes an EM would keep." The manager is an LLM — it reads and writes natural language better than JSON. A structured format would add parsing complexity with no benefit when the consumer is an LLM.

**Why inject context in the consumer, not let the manager search memory itself?**
The manager *could* read its own memory file on every event batch. But:
- It adds a tool call round-trip to every batch processing
- The manager might forget to check or skip it when busy
- Injecting context is deterministic — the consumer always does it

**Why 500-char cap on per-person context?**
The manager's context window is finite. With 3 people messaging simultaneously, injecting full conversation history would consume too much context. 500 chars per person (~5-8 lines of notes) is enough for the manager to recall context without crowding out the actual events.

**Why not track stakeholders in a separate file?**
One memory file is simpler. The manager already has guidance to use `memory.md`. Adding a second file means two places to read/write/prune. The section headers (`## Stakeholders`, `## Active conversations`) provide sufficient organization.

## Verification Plan

### Level 1: Unit tests

- `test_extract_person_context`: Given a memory string with multiple people mentioned, verify correct extraction and 500-char cap
- `test_write_events_file_with_context`: Given events with Slack senders and a memory file, verify the output includes a `## Conversation context` section with per-person excerpts
- `test_write_events_file_no_memory`: Given Slack events but no memory file, verify graceful fallback (no context section, no crash)
- `test_write_events_file_no_slack`: Given non-Slack events, verify no context section is appended

### Level 2: Integration tests

- Start the consumer with a mock memory file, push Slack events from two different senders through the bus, verify the pending_events.md file contains separate context sections for each sender
- Verify that task tracker events (no sender) don't trigger context injection

### Level 3: Manual QA

- Deploy to the remote box. Have 2-3 people message modabot on Slack simultaneously about different tickets
- Verify the manager responds to each person with appropriate context (doesn't mix up conversations)
- File a ticket, then have someone ask about it on Slack. Move the ticket to In Review. Verify the asking person gets a proactive update
- Have someone mention a ticket in a channel. Verify the update goes back to the channel, not a DM
- Wait >24h and verify stale conversation entries are pruned from memory

## Implementation Plan

### Step 1: Manager prompt — multi-user sections

**File:** `manager/prompt.md`

Add three new sections after "How you work" (after line 94):
1. `## Multi-user awareness` — per-person context switching behavior
2. `## Stakeholder tracking` — memory structure, when to note associations, pruning rules
3. `## Proactive updates` — when/how/where to notify stakeholders on ticket state changes

Also update the memory guidance (around line 100) to define the memory file sections.

### Step 2: Startup prompt — remove hardcoded DM channel

**File:** `manager/session.py`

In `_inject_startup_prompt()`, replace the hardcoded Zach DM channel reference with multi-user aware language. The manager will discover channels from events.

### Step 3: Consumer — context injection

**File:** `manager/events/consumer.py`

1. Add `MEMORY_PATH` constant
2. Add `_read_memory()` helper
3. Add `_extract_person_context()` helper
4. Modify `_write_events_file()` to append `## Conversation context` section when Slack events have senders and memory exists

### Step 4: Tests

**File:** `tests/test_consumer.py` (new or extend existing)

Add unit tests for `_extract_person_context` and the enriched `_write_events_file` output.

### Step 5: Manual validation

Deploy, have multiple people message the bot, verify context separation and proactive updates work as described in Level 3 QA.
