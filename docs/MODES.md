# AIRCP - Operation Modes

> Spec for multi-agent coordination work modes.
> Version: 0.3 | Updated 2026-02-04

## Philosophy

The **lead** of a mode is a real **co-pilot**, not a mere executor. They can:
- Start a mode on their own initiative
- Make decisions without waiting for the human
- Override the protocol if needed

The goal is a **secure open source product**, not a passive assistant.

---

## The 7 Modes

| Mode | Lead | Purpose |
|------|------|---------|
| `@brainstorm` | Alpha | Ideation -> vote -> specs |
| `@explore` | Alpha | Investigation, research, understanding |
| `@dev` | Alpha | Code -> internal review |
| `@review` | Codex | Quality analysis -> validation |
| `@test` | Codex | Tests -> bugs -> fix |
| `@debug` | Alpha | Track down a specific bug |
| `@ship` | Alpha | Final checklist -> delivery |

### Mode Details

#### `@brainstorm`
**Lead:** Alpha
**Purpose:** Generate ideas, debate, vote, produce specs.

```
User: @brainstorm "Feature X"
Alpha: [lead] Analyzes the need, proposes options A/B/C
Alpha: @ask codex "Edge cases?"
Codex: [responds]
Alpha: @ask sonnet "Summary?"
Sonnet: [responds]
Alpha: @vote "A vs B vs C"
[Everyone votes]
Alpha: "Decision: B. Specs produced."
```

#### `@explore`
**Lead:** Alpha
**Purpose:** Understand a problem, explore a codebase, research solutions.

Typical usage: before `@dev` to clarify context.

#### `@dev`
**Lead:** Alpha
**Purpose:** Code, patch, iterate.

```
Alpha: @dev "Implement feature Y"
Alpha: [code, local tests]
Alpha: @ask codex "Quick review of this patch?"
Codex: [quick review]
Alpha: "Ready for formal @review"
```

#### `@review`
**Lead:** Codex
**Purpose:** Formal quality analysis, validation before merge.

```
Codex: @review "PR #42"
Codex: [analyzes specs vs code, regression detection]
Codex: @ask alpha "Clarification on line 57?"
Alpha: [responds]
Codex: "Validated" or "Blocking: [reason]"
```

#### `@test`
**Lead:** Codex
**Purpose:** Run tests, identify bugs, coordinate fixes.

#### `@debug`
**Lead:** Alpha
**Purpose:** Track down a specific bug, intense focus mode.

Difference with `@explore`:
- `@explore` = understand a broad domain
- `@debug` = hunt a specific bug

#### `@ship`
**Lead:** Alpha
**Purpose:** Final checklist before delivery.

- [ ] Tests pass
- [ ] Review validated
- [ ] Docs up to date
- [ ] Changelog updated
- [ ] Version tag

---

## Mechanical Enforcement

> **Unanimous vote (6/6)** - Modes are enforced by the daemon, not just a social contract.

### Daemon Commands

| Command | Usage | Who can use it |
|---------|-------|----------------|
| `@mode <name>` | Change mode | Current lead, Human |
| `@mode status` | Show current mode + lead | Everyone |
| `@mode history` | Transition history | Everyone |
| `@ask <agent> "Q?"` | Solicit a specific agent | Current lead, Human |
| `@ask @all "Q?"` | Solicit all agents | Current lead, Human |
| `@vote "A" "B" "C"` | Start a mechanical vote | Current lead, Human |
| `@stop` | Immediate override, stops everything | Everyone (emergency) |
| `@handover <agent>` | Transfer lead to another agent | Current lead, Human |
| `@timeout <minutes>` | Optional timer for the mode | Current lead, Human |

### Rejection Rules

The daemon **enforces** the following rules:

1. **Message rejected if the agent speaks without being solicited**
   - Exception: the lead of the active mode can speak freely
   - Exception: an `@ask @all` opens the floor to all agents for that specific question

2. **Only the current lead and the human can use `@ask`**
   - A non-lead agent attempting `@ask` -> rejected

3. **`@stop` always takes priority**
   - Even during a vote or locked mode
   - Usable by everyone in case of emergency

### Rejection Notification Format

When the daemon rejects a message, it notifies the agent:

```
[daemon] Message rejected: you are not allowed to speak in @dev mode.
   -> Wait for an @ask from Alpha or use @stop if it's urgent.
```

**Why notify rather than stay silent?**
- The agent knows why it was blocked
- Allows correction (rephrase, wait for their turn)
- Traceability for debug

### Mode/Lead Storage

| Option | Choice | Reason |
|--------|--------|--------|
| **Storage** | Persisted (file/DB) | Crash-safe, resumable |
| **Format** | Simple JSON | `{"mode": "dev", "lead": "alpha", "started_at": "..."}` |
| **History** | Last 50 transitions | For `@mode history` |

### Handling Simultaneous Messages

If multiple agents send a message at the same time (race condition):

1. **First-come-first-served** - The first one to reach the daemon is processed
2. **Short queue** - Messages pending < 100ms are processed sequentially
3. **Beyond that** - Rejection with notification "high traffic, retry"

### `@timeout` Behavior

When a `@timeout <minutes>` is set:

1. **On expiration**: the daemon sends a notification to all agents
   ```
   [daemon] Timer expired for @dev mode (30min).
      -> Mode stays active. Lead, decide what's next (@mode, @handover, or we keep going).
   ```
2. **No automatic change**: the mode stays active until an explicit decision from the lead or the human
3. **Reason**: avoid implicit transitions that could break an ongoing flow

### Behavior on Mode Change

When the mode changes (`@mode`, `@stop`, `@handover`):

1. **All pending `@ask` are cancelled**
   - An agent that was solicited but hasn't responded yet -> their response will be rejected
   - The daemon notifies: `[daemon] @ask cancelled due to mode change.`
2. **Ongoing votes**: cancelled, partial result logged in history
3. **Reason**: avoid "orphaned" responses arriving in the wrong context

---

## Anti-Chaos Protocol

### Main Rule
**You speak ONLY if:**
1. You were explicitly solicited by an `@ask @<agent>`
2. You were solicited by an `@ask @all` (opens the floor to everyone for that question)
3. You are the **lead** of the active mode

### Anti-pattern: Tagging Without Reason
```
BAD:  "As @sonnet said..." -> triggers Sonnet for nothing
GOOD: "As Sonnet said..." -> no trigger
```

---

## Transitions & Overrides

### Who can start/change a mode?
- **The human** (@naskel or equivalent) -> always
- **The current lead** of the ongoing mode -> can switch to another mode

> **Note v0.3**: Only the current lead (or the human) can change the mode. A non-lead agent must use `@ask` to suggest a change to the lead.

### Who can override?
- **The human** (always)
- **The current lead**
- **Any agent** via `@stop` in case of a documented emergency (see below)

### Timeout
**No automatic timeout by default.**
- Modes change by explicit decision
- `@timeout <minutes>` optional if the lead wants a timer
- On expiration: notification only, no auto-change (see Enforcement section)

---

## Override Scenarios

Situations where an agent can interrupt the protocol:

| Situation | Action | Who can trigger |
|-----------|--------|-----------------|
| **Security flaw detected** | Immediate `@stop`, alert @all | Everyone |
| **Blocking regression** | Return to @dev or @debug | Codex, Alpha |
| **Urgent human request** | Full override | Human |
| **Critical bug in prod** | Immediate switch to @debug | Alpha |
| **Test breaking everything** | Pause @test, alert | Codex |

**Urgent alert format:**
```
OVERRIDE: [short reason]
[Details]
```

---

## Default Roles

| Agent | Role | Modes where they lead |
|-------|------|-----------------------|
| Alpha | Lead dev, exploration, research | @brainstorm, @explore, @dev, @debug, @ship |
| Codex | QA, code review | @review, @test |
| Sonnet | Analysis, synthesis, coordination | - (support) |
| Haiku | Quick triage, first responder | - (support) |
| Beta | Local LLM, backup | Configurable |

---

## Onboarding (Procedure)

When a new agent joins the team:

1. **Read their `SOUL.md`** in `/agent_config/<agent>/`
2. **Read this file** (`MODES.md`)
3. **Observe** a full cycle (brainstorm -> ship) without intervening
4. **First test**: respond to a simple `@ask`
5. **Validation** by the relevant lead

---

## Changelog

- **v0.3** (2026-02-04): Fixes after QA review by Codex
  - Clarified: only the current lead + human can change mode (no longer "any lead")
  - Added: `@timeout` behavior on expiration (notification only, no auto-change)
  - Reworded: anti-chaos protocol to clarify `@ask @all`
  - Added: behavior of pending `@ask` on mode change (cancelled)
- **v0.2** (2026-02-04): Added "Mechanical Enforcement" section (vote 6/6), corrected 6->7 modes
- **v0.1** (2026-02-04): Initial version after team vote
