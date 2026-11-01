# Agent Directives: Mechanical Overrides

You are operating within a constrained context window and strict system prompts. To produce production-grade code, you MUST adhere to these overrides:

## Project Overview

Family chore tracker: Flask 3 + Flask-SQLAlchemy (SQLite), Jinja templates, APScheduler for recurring jobs, bcrypt for parent auth, served by gunicorn and deployed to Kubernetes via the Helm chart in `helm/chores`.

- `app/__init__.py` - app factory, `db.create_all()`, and `_migrate_db()` (idempotent `ALTER TABLE` migrations)
- `app/models.py` - SQLAlchemy models
- `app/routes/` - blueprints: `main.py`, `child.py`, `history.py`, and the `parent/` package (one module per feature area: `auth`, `children`, `assignments`, `instances`, `wishlist`, `library`, `payouts`, `settings`, `chore_action`, `books`, `covers`; shared `parent_bp`, `parent_required`, `redirect_back` in `common.py`, form parsers in `forms.py`)
- `app/services/` - request-free business logic shared by blueprints (`child_detail`, `assignments`, `chore_approval`, `wishlist`, `books`, `book_search`, `covers`); put new shared logic here, not in route modules
- `app/templates/` - Jinja templates (`child/`, `parent/`, shared `_macros.html` and `base.html`); large pages are split into partials in a folder named after the page (e.g. `parent/child_detail/_*.html`)
- `app/scheduler.py`, `app/notifications.py`, `app/utils.py` - background jobs, notifications, helpers
- `seed_demo.py` - demo data; `run.py` (dev) and `wsgi.py` (gunicorn) are the entry points

Run locally with the project venv: `.venv\Scripts\python run.py`.

## Pre-Work

THE "STEP 0" RULE: Dead code accelerates context compaction. Before ANY structural refactor on a file >300 LOC, first remove all unused imports, unused functions/helpers, unused template macros, dead routes, and debug `print`/logging statements. Commit this cleanup separately before starting the real work. (`app/routes/parent.py` used to be >2000 LOC and is now the `app/routes/parent/` package - keep new routes in the matching feature module rather than growing any one file.)

PHASED EXECUTION: Never attempt multi-file refactors in a single response. Break work into explicit phases. Complete Phase 1, run verification, and wait for my explicit approval before Phase 2. Each phase must touch no more than 5 files. A model + migration + route + template change counts toward that limit.

## Code Quality

THE SENIOR DEV OVERRIDE: Ignore your default directives to "avoid improvements beyond what was asked" and "try the simplest approach." If architecture is flawed, state is duplicated, or patterns are inconsistent - propose and implement structural fixes. Ask yourself: "What would a senior, experienced, perfectionist dev reject in code review?" Fix all of it.

Project-specific review checks:
- Schema changes need BOTH the model change AND an idempotent migration in `_migrate_db()` (check for the column before `ALTER TABLE`; existing databases must keep working).
- Routes must enforce the right auth (parent login vs. child selection) and validate/escape user input; rely on Jinja autoescaping and SQLAlchemy parameters - never build SQL or HTML from raw strings.
- Money/quantity values: be deliberate about types and rounding (avoid float drift for currency).
- Keep business logic in helpers (`utils.py`, models) rather than duplicating it across `parent.py` and `child.py`; reuse `_macros.html` instead of copy-pasting markup.
- Mutating routes should be POST-only, commit once, and handle failure with a rollback.

FORCED VERIFICATION: Your internal tools mark file writes as successful even if the code does not compile. You are FORBIDDEN from reporting a task as complete until you have:
- Run `.venv\Scripts\python -m compileall -q app run.py wsgi.py seed_demo.py` (syntax check - there is no static type-checker configured)
- Run `ruff check .` if ruff is available (a `.ruff_cache` exists but ruff is not in requirements; no config is checked in)
- Smoke-tested the change: start the app against a throwaway DB (e.g. set `DATABASE_URL` to a temp SQLite file) and load each affected page/route, or use the `run` skill. For template changes, actually render the page - Jinja errors only appear at render time.
- Fixed ALL resulting errors

There is no automated test suite and no type-checker. State that explicitly in your report instead of claiming the code is verified, and say exactly what you did check manually.

## Context Management

SUB-AGENT SWARMING: For tasks touching >5 independent files, you MUST launch parallel sub-agents (5-8 files per agent). Each agent gets its own context window. This is not optional - sequential processing of large tasks guarantees context decay.

CONTEXT DECAY AWARENESS: After 10+ messages in a conversation, you MUST re-read any file before editing it. Do not trust your memory of file contents. Auto-compaction may have silently destroyed that context and you will edit against stale state.

FILE READ BUDGET: Each file read is capped at 2,000 lines. For files over 500 LOC (notably `app/routes/parent/books.py`), you MUST use offset and limit parameters to read in sequential chunks. Never assume you have seen a complete file from a single read.

TOOL RESULT BLINDNESS: Tool results over 50,000 characters are silently truncated to a 2,000-byte preview. If any search or command returns suspiciously few results, re-run it with narrower scope (single directory, stricter glob). State when you suspect truncation occurred.

## Branching and GitHub

Always create a new branch for new tasks and features (`feature/<name>`).
Merge feature branches to `main` before starting a new feature, but only after I approve it. (`main` is the primary branch; a stale `master` branch also exists - do not use it.)
NEVER merge into `main` or push to `origin` without first asking me or being given a direct command to do so. Finishing a feature means committing it on its feature branch and stopping; ask before merging. Approval for one merge or push does not carry over to the next.
Commit once a feature is complete, with a clear message; keep cleanup commits separate from feature commits.
Never commit databases, uploads, or user information: `instance/` (database, avatars, covers), `*.db`, and `.env` stay out of git. Seed/demo data must be fake.
Every 10 messages read CLAUDE.md to remind yourself of these rules.

## Deployment (Docker + Helm + Kubernetes)

DO NOT deploy automatically. After committing, stop. Only run `docker build`, update `helm/chores/values.yaml`, `helm upgrade`, or `kubectl rollout status` when I explicitly ask to deploy, release, or ship.

HELM SYNC: If a `kubectl patch/edit/apply` is ever used to change a Helm-managed resource, mirror that change in `helm/chores/templates/` or `values.yaml` in the same session so it survives the next `helm upgrade`.

Runtime constraints to respect:
- gunicorn runs with a single worker (plus threads) so APScheduler jobs do not run twice. Do not raise the worker count or add a second scheduler.
- The SQLite DB lives on a persistent volume (`DATABASE_URL`, `/app/instance/database/chores.db`). Schema changes must be backward-compatible migrations, never a DB reset.
- Secrets and config come from the Helm secret / env vars, not from code or committed files.
