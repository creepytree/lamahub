# Lamahub — agent reference

Lamahub is a **consumer of the `druids` design framework** (pip name `druidforms`,
import name `druids`). It is pure Python: FastAPI + Jinja. It ships **no JS build
step** — all design, theming, the app shell, login/session and every `<druid-*>`
element come from the installed `druids` package.

## On resuming edits

1. **Create a venv** and activate it: `python -m venv .venv && . .venv/bin/activate`.
2. **Update the framework** into it from its git URL: `pip install "druidforms @ git+<framework-repo-url>"`
3. **Study the CHANGELOG.md** Compare local version with latest pull and check if the project needs patches on the new version or would gain quality, simplification or a reduction in line-count by patching.
4. **Add bugs, gaps, wanted patches to GAPS.md** This gets consumed by the Agent processing the framework. Overwrite with fresh content on a new edit roundtrip if the file notes a resolved state.

## Startup new project

**On the first turn, before writing any UI, install the framework and study it:**

1. **Create a venv** and activate it: `python -m venv .venv && . .venv/bin/activate`.
2. **Install the framework** into the venv `pip install "druidforms @ git+<framework-repo-url>"`
3. **Study the framework** in the venv `<site-packages>/druids/AGENTS.md` — the API
   contract (every `<druid-*>` component, `df-*` class, design token and the
   `window.druids` JS API). Build UI only from what it documents.
4. Write AGENTS.consumer.md in the workspace root of the consumer
5. Write README.consumer.md for the consumer, @placeholder@ define allowed changes, keep it strict on this

## Do always

> **Build on the framework, never reinvent it.** Before adding markup, CSS or JS, check
> whether druids already provides it: a `<druid-*>` component, a `df-*` class, a design
> token (`--accent`, `--border`, `--bg-raised`, `--radius`, …) or `druids.toast()` /
> `druids.applyAccent()`. App CSS must theme with those tokens, not hardcoded colors,
> and must not re-implement a component the framework already ships.
>
> **Keep this app matching the framework's current API.** The catalog above is the
> source of truth. If a druids component, attribute, event or class was renamed or
> removed upstream, update this app's templates/CSS/JS to match in the same change.
>
> **Missing or wrong in the design system → fix it upstream, not here.** If a UI need
> isn't met, add or change the component in the `druids` framework repo (rebuild its
> bundle there) rather than growing a local one-off. Only genuinely app-specific UI
> lives in this app.

## Layout

- `lamahub/shell.py` — the single `Druids(...)` instance (brand, version, login from
  `LOGIN`/`LOGIN_USER`/`LOGIN_PW` env, `templates_dir`); `lamahub/app.py` calls
  `druids.install(lamahub)` and mounts the app's own `/static`.
- `lamahub/routes.py` — pages render via `druids.templates`; `lamahub/api.py` is the
  JSON/streaming API under `/api` (models, chat, logs, endpoints).
- `lamahub/events.py` — Socket.IO handlers; handshakes are auth-checked against
  `druids.auth` because Socket.IO is mounted outside the FastAPI middleware stack.
- `lamahub/templates/main.jinja2` — the single page; extends `druids/base.jinja2` and
  fills the `styles`, `actions`, `content`, `scripts` blocks with `<druid-*>` tags
  (the `<druid-tabs>` strip lives above the panels in `content`, not in the navbar;
  tabs: Models / Prompt / Log; the Log tab is a plain `<druid-log-view>`).
- `lamahub/static/css/app.css` — only app-specific UI (dashboard grid, models table,
  chat sizing, markdown rendering), all token-driven (`--accent`, `--border`, …).
  Framework CSS is prefixed `df-`; app class names are prefixed `lh-`.
- `lamahub/static/js/` — app logic (`app.js`, `models.js`, `chat.js`, `endpoints.js`,
  `utils.js`, `requests.js`, `icons.js`) plus vendored `socket.io`, `markdown-it`,
  `highlight.js`, `sortable`. Notifications go through `druids.toast()`; confirm/prompt
  use `druids.confirm()` / `druids.prompt()` (no native `alert`/`confirm`/`prompt`);
  icons are Lucide SVGs registered via `druids.registerIcons()` in `icons.js` and
  referenced as `<druid-icon name>` / `<druid-icon-button icon>`; generated markup
  uses `<druid-*>` elements and `lh-*` classes.
- `lamahub/services/` — Ollama client, endpoint registry, fixed-model store, logging.
