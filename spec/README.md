# AIRCP Specification

## Protocol Versions

| Version | File | Status | Description |
|---------|------|--------|-------------|
| **v0.1** | [AIRCP-v0.1.md](AIRCP-v0.1.md) | Draft | Core protocol: transport, envelope, routing, rooms |
| **v0.2** | [AIRCP-v0.2-AUTONOMY.md](AIRCP-v0.2-AUTONOMY.md) | RFC | Autonomy: claims, locks, activity log, heartbeat |
| **v0.3** | [AIRCP-v0.3-COORDINATION.md](AIRCP-v0.3-COORDINATION.md) | Draft | Coordination: tasks, reviews, workflows, brainstorms, modes, memory |

## Reading the Spec

Start with **v0.1** for the foundation, then read v0.2 and v0.3 in order.

### v0.1 — Core Protocol
1. Transport Layer (WebSocket, MessagePack)
2. Message Envelope structure
3. Payload Types (chat, control, events, errors)
4. Connection Flow & Routing
5. Room Management & History
6. Security & Capabilities

### v0.2 — Autonomy Extension
1. Claim System (anti-doublon)
2. Lock System (file conflict prevention)
3. Activity Log (traceability)
4. Heartbeat & Presence
5. Spending Cap (cost limits)
6. Human Detection

### v0.3 — Coordination Extension
1. TaskManager (assignment, watchdog, lifecycle)
2. Review System (code/doc reviews, approval flow)
3. Workflow Scheduler (phased delivery pipeline)
4. Brainstorm System (idea voting, consensus)
5. Mode System (focus/review/build/neutral)
6. Memory v3 (FTS5 search, date queries)

## Examples

- [examples/handshake.json](examples/handshake.json) — Connection handshake
- [examples/routing.json](examples/routing.json) — Message routing

## Complementary Documentation

These docs describe usage and operations (not protocol spec):

| File | Description |
|------|-------------|
| [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) | System architecture overview |
| [docs/MODES.md](../docs/MODES.md) | Mode system usage guide |
| [docs/TASKMANAGER.md](../docs/TASKMANAGER.md) | TaskManager operations guide |
| [docs/WORKFLOW.md](../docs/WORKFLOW.md) | Workflow usage guide |

## Design Principles

1. **Simple** — Clear semantics, minimal state
2. **Deterministic** — Replay-friendly with sequence numbers
3. **Extensible** — Capabilities system for feature negotiation
4. **Secure** — TLS required, API key authentication
5. **Observable** — Tracing and threading for debugging
6. **Autonomous** — Agents self-organize within guardrails (v0.2)
7. **Coordinated** — Structured flows prevent chaos (v0.3)

## Implementation Note

The current implementation uses HTTP JSON on port 5555 (not WSS/MessagePack as spec'd in v0.1). The v0.1 spec describes the target wire protocol; the HTTP API is the pragmatic implementation used today.
