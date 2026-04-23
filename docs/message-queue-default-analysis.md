# Hermes Message Queue vs Interrupt — Architecture Analysis & Implementation Plan

## Executive Summary

There is **no strong architectural reason** Hermes cannot default to queuing user messages instead of interrupting. The CLI already supports this via `busy_input_mode: queue`. The gateway has all the plumbing but currently only uses it during restart/stop drain. Making queue the default is a surgical change touching ~3 code sites.

---

## 1. How Interrupt Currently Works

### Agent Loop (`run_agent.py`)

The agent loop is synchronous inside an async executor thread. Interrupt is cooperative:

```python
# AIAgent.__init__
self._interrupt_requested = False
self._interrupt_message = None

# AIAgent.interrupt() — called from another thread (gateway message handler)
def interrupt(self, message=None):
    self._interrupt_requested = True
    self._interrupt_message = message
    _set_interrupt(True, self._execution_thread_id)  # signal in-flight tools
    # ... propagate to child agents

# Inside the streaming / API loop
for event in stream:
    if self._interrupt_requested:
        break

# After tool calls
if self._interrupt_requested:
    raise InterruptedError("Agent interrupted")
```

When interrupted, the agent returns a result dict with:
- `interrupted: True`
- `interrupt_message: "<user text>"`

### Gateway Busy Handler (`gateway/run.py`)

When a message arrives while `_quick_key in self._running_agents`:

```python
# _handle_active_session_busy_message
1. Stores the message in adapter._pending_messages[session_key]
2. Calls running_agent.interrupt(event.text)  # ALWAYS does this
3. Sends debounced ack: "⚡ Interrupting current task..."
```

### Base Adapter Fallback (`gateway/platforms/base.py`)

If the busy handler is not set or returns `False`:

```python
if event.message_type == MessageType.PHOTO:
    merge_pending_message_event(...)  # queue without interrupt
else:
    self._pending_messages[session_key] = event
    self._active_sessions[session_key].set()  # signal interrupt
```

### Post-Run Drain

After the agent run finishes (interrupted or not), two places drain pending messages:

1. **Gateway `_run_agent`** (lines 10627–10823): checks `result.get("interrupted")`, dequeues pending event or `interrupt_message`, then **recursively calls `_run_agent`** for the follow-up.

2. **Base adapter `_process_message_background`** (line 2032): after `_message_handler` returns, checks `self._pending_messages` and calls `_process_message_background` recursively.

Both paths work. The gateway path is used for interrupted runs; the base adapter path catches anything queued during normal completion.

---

## 2. Existing Queue Infrastructure

### CLI Queue Mode (`cli.py`)

The CLI already has `busy_input_mode`:

```python
# Keyboard handler
if self._agent_running:
    if self.busy_input_mode == "queue":
        self._pending_input.put(payload)     # queues for next turn
        print("Queued for the next turn: ...")
    else:
        self._interrupt_queue.put(payload)   # interrupts immediately
```

### Gateway Drain-Time Queue Mode (`gateway/run.py`)

```python
_busy_input_mode = "interrupt"  # class default

def _queue_during_drain_enabled(self):
    return self._restart_requested and self._busy_input_mode == "queue"

# During drain: messages are queued and ack'd with:
# "⏳ Gateway restarting — queued for the next turn after it comes back."
```

**Critical finding:** `_busy_input_mode` is loaded from config/env (`HERMES_GATEWAY_BUSY_INPUT_MODE` / `display.busy_input_mode`) but **only checked during drain**. During normal operation it is ignored.

### `/queue` and `/steer` Commands

- `/queue <prompt>` — stores in `adapter._pending_messages` without interrupting. Works today.
- `/steer <prompt>` — injects mid-run after next tool call. Also works today.

These prove the gateway can accept messages without interrupting.

---

## 3. Why Defaulting to Queue is Safe

| Concern | Reality |
|---------|---------|
| Agent might run forever before seeing queued message | Same as today — user can `/stop` or `/steer`. Max iterations (default 90) caps it. |
| Multiple rapid messages | `merge_pending_message_event()` already merges text/photos. Rapid queue-mode messages get concatenated with newlines, just like interrupt mode. |
| Role alternation violations | Queue mode does not insert user messages mid-run (unlike `/steer`). It waits for the turn boundary. Safe. |
| Streaming responses | In queue mode, the current stream completes naturally. The queued message starts a fresh turn after. No stale-response suppression issues. |
| Backward compatibility | Keep `busy_input_mode: interrupt` as an opt-in for users who want the old behavior. |

---

## 4. Implementation Plan

### 4.1 Change Default Config

**File:** `hermes_cli/config.py`  
Change:
```python
"busy_input_mode": "interrupt",
```
to:
```python
"busy_input_mode": "queue",
```

Also update:
- `cli-config.yaml.example`
- `website/docs/user-guide/cli.md`

### 4.2 Teach Gateway Busy Handler to Respect Queue Mode

**File:** `gateway/run.py` — `_handle_active_session_busy_message`

Current logic (normal busy case, lines 1624–1697):
```python
# --- Normal busy case ---
merge_pending_message_event(adapter._pending_messages, session_key, event)
running_agent.interrupt(event.text)   # ALWAYS interrupts
```

New logic:
```python
# --- Normal busy case ---
merge_pending_message_event(adapter._pending_messages, session_key, event)

if self._busy_input_mode == "queue":
    # Queue mode: do NOT interrupt. The pending message will be drained
    # automatically when the current turn finishes.
    _BUSY_ACK_COOLDOWN = 30
    now = time.time()
    last_ack = self._busy_ack_ts.get(session_key, 0)
    if now - last_ack >= _BUSY_ACK_COOLDOWN:
        self._busy_ack_ts[session_key] = now
        await adapter._send_with_retry(
            chat_id=event.source.chat_id,
            content="📝 Queued for the next turn.",
            reply_to=event.message_id,
            metadata=thread_meta,
        )
    return True
else:
    # Interrupt mode (legacy): abort current run
    running_agent = self._running_agents.get(session_key)
    if running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
        try:
            running_agent.interrupt(event.text)
        except Exception:
            pass
    # ... existing ack logic ...
```

### 4.3 Teach Base Adapter Fallback to Respect Queue Mode

**File:** `gateway/platforms/base.py` — `handle_message()` (lines 1746–1751)

Current fallback:
```python
# Default behavior for non-photo follow-ups: interrupt
self._pending_messages[session_key] = event
self._active_sessions[session_key].set()
```

This fallback only runs when `_busy_session_handler` is `None` or returns `False`. Since the gateway sets the handler, this is mainly a safety net for tests or custom adapters.

We should still make it queue-aware. The adapter does not know `_busy_input_mode`, so either:
- **Option A:** Pass the mode into the adapter constructor
- **Option B:** Make the busy handler responsible for ALL queue-vs-interrupt decisions, and remove the fallback interrupt entirely (replace with a queue + warning log)

**Recommended: Option B.** The fallback should queue by default and log a warning that no busy handler was registered. The gateway handler is always set in production.

### 4.4 Ensure Commands Still Bypass Queue/Interrupt

Commands like `/stop`, `/new`, `/approve`, `/deny`, `/yolo`, `/verbose`, `/background` already bypass the busy handler in `_handle_message` (lines 3325–3459). No changes needed.

However, we should verify that `/queue` and `/steer` still work correctly when queue mode is the default.

### 4.5 Update Documentation

**File:** `website/docs/user-guide/cli.md`

Update the busy input mode section:
- Default is now `"queue"`
- Explain that users who want immediate interrupt can set `"interrupt"`
- Mention `/stop` as the explicit interrupt mechanism

### 4.6 Add/Update Tests

**Files to touch:**
- `tests/gateway/test_restart_drain.py` — already tests `_load_busy_input_mode`; add test for normal-operation queue behavior
- `tests/cli/test_cli_init.py` — update default assertion from `"interrupt"` to `"queue"`
- Add new test in `tests/gateway/`:
  - Simulate a running agent
  - Send a message with `_busy_input_mode == "queue"`
  - Assert `agent.interrupt()` was NOT called
  - Assert pending message is stored
  - Simulate agent completion
  - Assert pending message is processed as next turn

---

## 5. Edge Cases & Mitigations

| Edge Case | Handling |
|-----------|----------|
| User sends 5 messages rapidly | `merge_pending_message_event()` merges them. In queue mode with `merge_text=True` they concatenate. |
| User queues then wants to stop | `/stop` command bypasses the queue and force-clears the session. |
| Agent is stuck in infinite loop | `/stop` still works. Queue mode does not remove any safety mechanism. |
| Gateway restart during queued message | Existing drain logic handles this — `_queue_during_drain_enabled()` already queues during restart. |
| Streaming platform (Discord, Telegram) | Queue mode is actually *better* for streaming — no partial stream cancellation. |

---

## 6. Files to Modify (Summary)

1. `hermes_cli/config.py` — change default
2. `gateway/run.py` — `_handle_active_session_busy_message` queue-mode branch
3. `gateway/platforms/base.py` — fallback queue behavior (safety net)
4. `cli-config.yaml.example` — update comment/default
5. `website/docs/user-guide/cli.md` — update docs
6. `tests/cli/test_cli_init.py` — update default test
7. `tests/gateway/test_restart_drain.py` or new file — queue-mode behavior test

---

## 7. Migration Path for Existing Users

Since this changes default behavior, users who relied on "type to interrupt" will need to opt in:

```yaml
# ~/.hermes/config.yaml
display:
  busy_input_mode: "interrupt"
```

The change is safe because:
- `/stop` is a more explicit and reliable interrupt anyway
- Queue mode reduces accidental disruption of long-running tasks
- The user can still guide the agent mid-run via `/steer`
