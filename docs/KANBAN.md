# StormPilot Project Kanban

The project board tracks engineering work. It is separate from the simulated dispatch endpoint and does not represent delivery records, aircraft, or real logistics activity.

## Workflow

| Status | Meaning | Allowed next statuses |
| --- | --- | --- |
| Backlog | Identified, not selected | To Do, Blocked |
| To Do | Selected and ready | In Progress, Blocked |
| In Progress | Actively being implemented | Code Review, Blocked |
| Code Review | Awaiting review or verification | Testing, In Progress, Blocked |
| Testing | Functional, integration, or security verification | Done, In Progress, Blocked |
| Done | Acceptance criteria verified and evidence recorded | Testing |
| Blocked | Work cannot continue; a reason is required | Backlog, To Do, In Progress, Code Review, Testing |

Transitions are enforced by the API for both `PUT /api/tasks/{id}` and `POST /api/tasks/{id}/move`. Moving to Done requires non-empty `acceptance_criteria` and `completion_evidence`; Done is not a code-written flag. Moving to Blocked requires `blocker_reason`. Leaving Blocked clears its reason. Returning Done to Testing reopens the work and clears its completion timestamp.

## Task data

Tasks retain their integer database `id`; the UI displays it as `SP-001` style. Fields include title, description, task type, priority, status, assignee, creation/update timestamps, optional due date, acceptance criteria, completion evidence/date, blocker reason, sample marker, and optional sprint/story references. Task types are Feature, Bug, Security, Testing, DevOps, Documentation, and Infrastructure. Dependencies and labels are not implemented in this MVP.

Existing SQLite databases are migrated additively when the application starts. Existing task IDs, tasks, and four legacy statuses are retained. The new columns receive safe defaults; existing `todo`, `in_progress`, `testing`, and `done` tasks remain in their current status.

## API

- `GET /api/board` returns all seven columns, task cards, WIP limits, and total/by-status/by-priority/blocked/completed counts.
- `GET /api/tasks` supports `status`, `priority`, `task_type`, `assignee`, `search`, `is_sample`, `sprint_id`, and `story_id` query filters.
- `POST /api/tasks`, `GET /api/tasks/{id}`, `PUT /api/tasks/{id}`, `DELETE /api/tasks/{id}`, and `POST /api/tasks/{id}/move` manage existing task records.
- `POST /api/tasks/samples` explicitly seeds missing examples. It is idempotent by sample title and never overwrites tasks. Seeded examples are marked `is_sample: true`, start in Backlog, and are not marked complete.
- `GET /api/wip-limits` and `PUT /api/wip-limits` read and configure per-column limits. A `null` limit is unlimited; zero prevents entry.

The UI search and filters update the visible board; the API filters can also be used directly. Column counts respect UI filters, while WIP limit badges and the board summary represent all tasks.

## Persistence and security

Local Python and Compose use SQLite. Compose mounts the `stormpilot-data` named volume at `/data`, so task data survives container recreation unless that volume is explicitly removed. Back up the volume before destructive cleanup.

The Kubernetes manifest mounts an `emptyDir` in each API pod and runs two replicas. Each pod therefore has a separate SQLite file; task writes can differ by pod and disappear when that pod is replaced. This is not suitable for a shared board. Do not claim multi-replica task persistence until the app is moved to a shared database and that behavior is integration-tested.

The project has no authentication or authorization. The task API inherits no user identity and accepts unauthenticated writes. Restrict use to a trusted local environment; do not expose the service publicly or use it as a team board. Add authentication/authorization before team use.

## Verification

Run the Python API suite:

```bash
python -m unittest discover -s tests -v
```

The tests cover migration-compatible task fields, CRUD, transition rejection, completion evidence, blocked reasons, search/filter validation, board summaries, WIP limits, sample idempotency, and persistence across SQLite connections. Docker/Compose and Kubernetes behavior must be verified separately in those environments.
