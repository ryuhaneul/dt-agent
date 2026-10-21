# Background Tasks

Delegate work that takes >30 seconds. The task agent runs autonomously in a
separate CLI session while you keep chatting with the user.

## When to use

- Research, browsing, comparisons (flights, hotels, products, etc.)
- File creation, documents, code generation
- Any multi-step work that would block the conversation
- Parallel independent sub-tasks

## When NOT to use

- Quick questions answerable in seconds
- Trivial one-line operations

## Creating a task

```bash
python3 tools/task_tools/create_task.py --name "Flight Search Paris" "Search available flights to Paris for 2 people in June. Compare prices and airlines."
```

Options:
- `--name NAME` — human-readable task name (recommended)
- `--provider PROV` — override provider (claude, codex, gemini, grok)
- `--model MODEL` — override model (opus, sonnet, flash, etc.)
- `--thinking LEVEL` — codex reasoning effort (low, medium, high)

**Important**: Include ALL context in the prompt. The task agent does NOT see
the conversation — give it everything it needs.

## Listing tasks

```bash
python3 tools/task_tools/list_tasks.py
```

## Cancelling a task

```bash
python3 tools/task_tools/cancel_task.py TASK_ID
```

## Resuming a completed task

Resume continues the task's CLI session — the agent keeps its full context
from the previous run. Use this instead of creating a new task when you want
to build on existing work.

```bash
python3 tools/task_tools/resume_task.py TASK_ID "now search only 2-week trips"
```

Runs on the **original provider/model**, regardless of current chat provider.

The resumed run reports to the chat/topic you resume it from, even if the task
was created in another topic: its result, any `ask_parent` question and the
answer you then send use that run's destination, which survives a restart.
An unanswered question captured during an earlier run keeps its earlier destination.
Resuming again from the original topic moves the next run back.

**When to resume vs. create new:**
- **Resume**: Refine results, follow-up questions, adjusted parameters,
  answer a question posted in chat after the task has finished that run
- **New task**: Completely different work, unrelated to any previous task

### Resume examples

1. Task searched Python best practices → user wants more on testing
   → `resume_task.py TASK_ID "Now focus on testing best practices in detail"`

2. Task question "For when?" is posted in chat → user says "June"
   → after that task run finishes, use
   `resume_task.py TASK_ID "Travel period: June 2026, departing from FRA"`

3. Task found flight options → user wants cheaper alternatives
   → `resume_task.py TASK_ID "Search cheaper alternatives, budget max 300 EUR"`

## Answering task questions (for parent agents)

A running task may ask through a private question turn in the source conversation.
In that turn, reply with the answer only; the reply goes to the task so it can
continue. If the answer needs the user's decision or approval, or information
you do not have, reply with a line containing only `NEEDS_USER`.

For a question posted in chat, obtain any missing user input, wait until the task
has finished that run, and answer with `resume_task.py TASK_ID "your answer"`.
A running task cannot be resumed. Your normal chat reply does not reach the task.

## Task folders are temporary

A task's list entry and its task folder (including `TASKMEMORY.md`) are working storage, not an
archive:

- Finished tasks are pruned automatically by age (`tasks.finished_retention_hours`, default 168
  hours) and by count (`tasks.finished_keep_last`, default 100). Either limit alone can remove an
  entry, so 168 hours is not a guaranteed minimum.
- The user can delete all finished tasks from `/tasks` (after a confirmation).

When you create a task, tell it in the prompt to save anything that must be kept outside its task
folder, for example in `output_to_user/`, the project folder or `memory_system/`.

## Inside a task (for task agents only)

When running as a background task agent, you can ask the parent agent:

```bash
python3 tools/task_tools/ask_parent.py "your question"
```

Set the command timeout to at least 300 seconds (300000 ms) and wait up to
about five minutes.
- If the output starts with `Answer from parent:`, continue with that answer.
- If it starts with `Question forwarded`, or the command fails or times out,
  finish what you can, include the unresolved question in your final result,
  and end this run. The parent will resume you with the answer after the run ends.
- Record each question and any answer in `TASKMEMORY.md` and your final result.
