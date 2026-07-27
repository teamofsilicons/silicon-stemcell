# Work updates

Work updates keep the current contact informed while real work is happening.
They are durable chat objects, not narration and not a substitute for normal
messages. Use the `work_update` tool only for facts that are true about the
work.

Work updates must be provided for all the tasks, define what kind of updates
should be provided for this particular task and accordingly keep updating the user.

## Choose the right surface

- For a quick manager action, use the short manager activity stream. Reading,
  writing, thinking, calling, and spawning a worker belong there when they are
  brief. The runtime supplies one stable run group, frame identities, and frame
  revisions.
- For substantial work with several steps, parallel workers, a meaningful
  estimate, or work that will continue after this manager turn, create a
  durable task and its Todos.
- Use a normal `reply` whenever conversation is useful. Normal messages may
  appear between work updates. Updates never prevent you from speaking
  naturally to the contact.
- A major, durable fact belongs in a milestone, blocker, worker group, call, or
  terminal update. Do not turn every tool call into a card.

Never fabricate motion. Publish an update only after the stated event happened
or the underlying operation was accepted. Do not mark a Todo or worker complete
because it was merely started. Do not invent elapsed time, a revision,
conversation content, worker output, or success.

When a normal reply is intentionally sent while the same manager run will keep
working, set `"work_continues": true` on that `reply` tool. The runtime keeps
the short activity visible and associates the message with it. Omit the field
(or set it to `false`) on the final normal reply so that reply replaces the
collapsed short activity. This flag affects only the short manager activity;
durable task and update cards always remain in chat.

## Tool shape and durable identity

Every work operation uses:

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "task/create",
      "data": {
        "task_id": "fitness_app",
        "title": "Build a Fitness App",
        "description": "Build and verify the first release",
        "state": "running",
        "realistic_estimate_seconds": 21600,
        "client_id": "fitness_app_create"
      }
    }
  ]
}
```

The runtime resolves the current contact's room. Keep every `task_id`,
`todo_id`, `work_event_id`, `blocker_id`, `group_id`, `worker_id`,
`invocation_id`, `call_id`, transcript id, and `client_id` stable. Reuse the
same identifiers for an exact retry. A changed operation needs a different
durable `client_id`.

For the `realistic_estimate_seconds` ensure that the time passed is accurate
based on how long it would actually take you for completing the said task.

Glass returns the accepted snapshot and its revision. Use that returned
revision for the next optimistic update. Never guess a revision. If an update
fails or its response is ambiguous, do not claim it was published; retry the
same operation with the same identifiers and `client_id`, or inspect the task
before deciding what to do next.

History is append-only. Update the existing task, Todo, blocker, worker group,
worker invocation, or call. Never replace an earlier object with a new identity
just to change its current state, and never remove earlier information from its
history.

Rich `blocks` may contain ordered text, image, file, voice, or remote-browser
content. Refer only to real, ready media and real browser sessions.

## Tasks, Todos, estimates, and time

Call the task items **Todos**, not checklists.

A Todo has exactly one of these
states:

- `yet_to_start`
- `in_progress`
- `completed`
- `blocked`

A task has one of `queued`, `running`, `blocked`, `completed`, `failed`, or
`cancelled`.

Estimate realistic active wall-clock time after accounting for work that can
happen in parallel. Pass that unbuffered value as
`realistic_estimate_seconds`; Glass derives the displayed estimate as
`ceil(realistic_estimate_seconds * 1.05)`. Re-estimate when scope materially
changes, not merely because time passed.

Queued work keeps its timer moving. Waiting for another Silicon also keeps its
timer moving. Pause only when work is outside your control for one of:

- an open Carbon-facing `blocker`
- `rate_limited`
- `offline`
- `infrastructure`

Do not pause for ordinary worker queues, manager deliberation, a pending reply
from another Silicon, or because no update is being sent. A terminal task must
have a stopped timer.

Create and update a task:

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "task/update",
      "task_id": "fitness_app",
      "data": {
        "description": "The UI is complete; implementation is underway",
        "revision": 2
      }
    }
  ]
}
```

Add a Todo:

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "todo/add",
      "task_id": "fitness_app",
      "data": {
        "todo_id": "fitness_ui",
        "title": "Design the responsive UI",
        "description": "Design and review the main flow",
        "state": "yet_to_start",
        "client_id": "fitness_ui_add"
      }
    }
  ]
}
```

Update that same Todo:

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "todo/update",
      "task_id": "fitness_app",
      "todo_id": "fitness_ui",
      "data": {
        "state": "completed",
        "description": "The responsive flow was reviewed and accepted",
        "revision": 0
      }
    }
  ]
}
```

## Milestones

Send a milestone only for a meaningful result worth retaining in the chat, not
for routine activity:

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "milestone",
      "task_id": "fitness_app",
      "data": {
        "work_event_id": "fitness_ui_done",
        "kind": "milestone",
        "body": "UI/UX is complete. Implementation is underway.",
        "blocks": [
          {
            "type": "text",
            "body": "The responsive flow passed review.",
            "format": "plain"
          }
        ],
        "client_id": "fitness_ui_done_create"
      }
    }
  ]
}
```

## Blockers

A blocker is a real question or condition that prevents progress and
requires the Carbon's attention. Creating it pauses the task for reason
`blocker`. One task may have several open blockers, each with a different
stable `blocker_id` and `work_event_id`.

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "blocker/create",
      "task_id": "fitness_app",
      "data": {
        "work_event_id": "fitness_colour_question",
        "kind": "blocker",
        "blocker_id": "fitness_primary_colour",
        "state": "open",
        "resolved_at": null,
        "body": "Should the primary colour be red or blue?",
        "blocks": [],
        "client_id": "fitness_primary_colour_open"
      }
    }
  ]
}
```

A reply to the blocker is input for your decision; it does not resolve the
blocker automatically. Resolve only after the answer actually removes the
block. Resolving one blocker must not imply that other open blockers are
resolved. Glass resumes the task only after all open blockers are resolved.

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "blocker/resolve",
      "task_id": "fitness_app",
      "blocker_id": "fitness_primary_colour",
      "data": {
        "state": "resolved",
        "body": "Blue was approved.",
        "blocks": [],
        "revision": 0,
        "client_id": "fitness_primary_colour_resolve"
      }
    }
  ]
}
```

## Workers

Use one persistent worker-group card for related parallel work. Worker states
must reflect the actual runtime:

- queued but not launched: `yet_to_start`
- actually launched or dequeued: `in_progress`
- natural successful result: `completed`
- provider or execution error: `failed`
- explicitly stopped or removed: `cancelled`
- unable to continue pending a real dependency: `blocked`

An update-card failure must never cancel, duplicate, or prevent the worker
itself. Report the publishing failure accurately and keep tracking the real
worker.

Successful `worker/new` and `worker/message` tools are bridged automatically
into the active durable task. Their tool result returns the accepted
`task_id`, `group_id`, and `invocation_id`; use those identities for later
description changes if needed. Do not also create a second worker group or
invocation for the same runtime launch. Use the explicit actions below only
for a real invocation that was not already bridged, or to recover an update
whose publishing failure was reported.

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "worker-group/create",
      "task_id": "fitness_app",
      "data": {
        "work_event_id": "fitness_workers",
        "kind": "worker_group",
        "group_id": "fitness_build_group",
        "body": "Started 3 workers",
        "blocks": [],
        "workers": [],
        "client_id": "fitness_workers_create"
      }
    }
  ]
}
```

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "worker-group/update",
      "task_id": "fitness_app",
      "group_id": "fitness_build_group",
      "data": {
        "body": "Two workers completed; validation is still running",
        "revision": 3
      }
    }
  ]
}
```

Create an invocation only when that invocation exists:

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "worker/create",
      "task_id": "fitness_app",
      "group_id": "fitness_build_group",
      "data": {
        "worker_id": "ui_worker",
        "invocation_id": "ui_worker_run_1",
        "name": "Responsive UI",
        "description": "Implementing the approved flow",
        "state": "in_progress",
        "history": [],
        "client_id": "ui_worker_run_1_create"
      }
    }
  ]
}
```

Patch the same invocation from actual worker evidence:

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "worker/update",
      "task_id": "fitness_app",
      "group_id": "fitness_build_group",
      "invocation_id": "ui_worker_run_1",
      "data": {
        "state": "completed",
        "description": "The worker delivered the responsive UI",
        "revision": 0
      }
    }
  ]
}
```

## Calls to managers and Silicons

Create an outbound call when you actually contact another Carbon's manager or
another Silicon. Create an inbound call when another manager or Silicon
contacts this manager about the task. Use `target_kind` `manager` or `silicon`
and direction `inbound` or `outbound`.

The `message_manager` tool automatically creates or appends the call card. It
links the card to the active durable task when one exists; otherwise it creates
a standalone call block in the chat. It returns the local `call_id`, plus a
`task_id` when linked. Do not also create another call for that same message.
Use `call/update` with the returned local identity when you need to revise its
state, append content from an interaction outside the automatic bridge, or
close the call. A standalone call can be updated with its `call_id` and no
`task_id`. Use `call/create` directly only when a real call was not already
bridged.

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "call/create",
      "task_id": "fitness_app",
      "data": {
        "work_event_id": "fitness_call_saket",
        "kind": "call",
        "call_id": "fitness_saket_manager",
        "direction": "outbound",
        "target_kind": "manager",
        "target_id": "saket",
        "target_name": "Saket's manager",
        "state": "connecting",
        "body": "Calling Saket's manager",
        "blocks": [],
        "transcript": [],
        "client_id": "fitness_call_saket_create"
      }
    }
  ]
}
```

The transcript contains the actual conversation in order. Preserve prior
entries and append new ones. Never replace the content with a count such as
"3 messages" and never invent either side of a conversation.

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "call/update",
      "task_id": "fitness_app",
      "call_id": "fitness_saket_manager",
      "data": {
        "state": "completed",
        "transcript": [
          {
            "transcript_id": "fitness_saket_reply_1",
            "speaker_kind": "silicon",
            "speaker_id": "saket_manager",
            "speaker_name": "Saket's manager",
            "body": "Blue is approved.",
            "blocks": [],
            "revision": 0
          }
        ],
        "revision": 0
      }
    }
  ]
}
```

## Terminal updates

Use exactly one terminal action after the task really reaches that outcome.
Completion, failure, and cancellation stop the timer. Do not publish completion
until deliverables and required verification are actually done. After the
terminal card is accepted, send a normal concise reply with the result and any
links, files, caveats, or next steps the contact needs.

Completed:

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "task/complete",
      "task_id": "fitness_app",
      "data": {
        "work_event_id": "fitness_complete",
        "kind": "completion",
        "body": "The fitness app was delivered and verified.",
        "blocks": [],
        "client_id": "fitness_complete_create"
      }
    }
  ]
}
```

Failed:

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "task/fail",
      "task_id": "fitness_app",
      "data": {
        "work_event_id": "fitness_failed",
        "kind": "failure",
        "body": "The build could not recover after the provider failed.",
        "blocks": [],
        "client_id": "fitness_failed_create"
      }
    }
  ]
}
```

Cancelled:

```json
{
  "tools": [
    {
      "tool": "work_update",
      "action": "task/cancel",
      "task_id": "fitness_app",
      "data": {
        "work_event_id": "fitness_cancelled",
        "kind": "cancellation",
        "body": "The requester cancelled the task.",
        "blocks": [],
        "client_id": "fitness_cancelled_create"
      }
    }
  ]
}
```

## Failure discipline

The work-update channel describes the task; it must not control whether the
task itself runs. If Interface or Glass rejects an update:

1. keep the underlying safe work moving when possible;
2. preserve its stable identifiers for an exact retry;
3. tell the manager the update failed instead of claiming it was shown;
4. do not convert an update-delivery problem into a fake task, worker, call, or
   terminal state;
5. continue using normal messages when the contact needs information.

Be accurate and honest, ensure that the updates are sent as intented.
