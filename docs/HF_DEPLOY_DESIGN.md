# HF sharded-GGUF deploy — design

Deploy any GGUF quant from a HuggingFace repo into a lamahub-managed Ollama server
over HTTP: **download shards → upload blobs → let Ollama assemble the split**. No
merge binary, no 2× disk — the shards-direct path is proven end-to-end against
Ollama 0.32.1 (a real 3-shard fixture uploaded as 3 blobs, `create`d with all
shards in the `files` map, assembled + ran + deleted clean).

Lives in a new **Deploy** tab beside Models / Prompt / Log.

---

## 1. HF Browse (top of the tab)

GGUF is exactly what Ollama ingests, so `filter=gguf` == "only Ollama-usable models"
(principled, not a scrape). All discovery uses HF's official public API — no anti-bot,
no HTML parsing.

### Search
- `GET https://huggingface.co/api/models?filter=gguf&search=<q>&sort=downloads&full=true`
- **Multi-string is native**: `search="qwen 122"` token-ANDs across the repo id
  (returns exactly Qwen-122B repos). The search box forwards the raw string.
- **Pagination = cursor / infinite scroll**: response carries
  `Link: <…&cursor=…>; rel="next"`. Fetch first page (`limit=N`), follow the cursor
  on scroll-to-bottom.
- Proxied through lamahub (`GET /api/hf/search?q=&cursor=`) so token/UA stay
  server-side and there's no CORS.

### Rate limits & caching (measured, anon)
- Metadata API (search, `blobs=true`): **500 req / 5 min** fixed window
  (`ratelimit-policy: q=500;w=300`).
- Download resolver (`/resolve/…`): **3000 req / 5 min** — far beyond a serialized
  deploy's needs.
- Comfortable, but cache anyway (server-side, in `hf_hub.py`):
  - **search** fires on an explicit **Search button + Enter** (no debounce/keystroke
    calls — same pattern as the Models "Pull" input); results cached by `(query, cursor)`,
    short TTL (~90 s).
  - **repo quant lists** by repo, keyed on the repo `sha`/`lastModified` for correctness
    (they rarely change), long TTL.
- Optional **`HF_TOKEN`** env → raises both limits and unlocks gated repos.

### Two-level view (one repo = ~27 quant variants, each its own size/shards)

**Level 1 — results scroller** (one search call, zero per-repo cost; all fields below
come free in the `full=true` response, incl. `siblings` filenames):

| name (→ huggingface.co/{id}) | provider (`author`) | capability (`pipeline_tag`: text/vision) | quants (count, "27 ▾") | updated (`lastModified`) | ↓ downloads |

No params column — it's readable from the model name; a regex/split can add it later
at zero cost if wanted. Sort: downloads / likes / updated.

**Level 2 — expand a repo** (lazy `GET /api/models/{repo}?blobs=true`, cached; adds
byte sizes that the search response omits):

| quant (Q4_K_M, UD-IQ2_M…) | size (Σ shard bytes) | shards (count) | **Deploy** |

Shard count + the quant list are derivable from the free search `siblings`; only the
byte **sizes** need the `blobs=true` call.

---

## 2. Deploy — 3 stages, detached background task + status poll

The deploy runs **server-side as a detached `asyncio` task** (not tied to the request),
so a browser reload or tab close does not abort a multi-GB/hour-long download. The
client `POST /api/hf/deploy` returns immediately (`{status: started|queued, id, position}`
or a refusal for a family already queued/running on that endpoint), then polls
`GET /api/hf/deploy/status` (~1 s) → `{active, current, queue, history}` to drive the
progress strip and the waiting list under it; on load it re-attaches to any still-running
deploy. A waiting job is removed with `DELETE /api/hf/deploy/queue/{id}`. Live progress
lives in the `hf_deploy.deploys` queue object (`DeployQueue`): `current`, waiting jobs in
`waiting`, the last 20 results in `history` (the client toasts each finished id once, so a
result isn't lost when the next job starts between two polls). `active` is True while the
single worker task drains the queue. Clicking Deploy on a quant runs:

1. **Downloading** — each shard streamed to the staging dir via HTTP Range (resumable);
   **sha256 computed in the same pass** (HF's blob id is git-sha1, unusable for Ollama —
   we hash locally, single read). Progress: per-shard %/bytes + overall N-of-M.
   HF resolve URL: `https://huggingface.co/{repo}/resolve/main/{subfolder}/{file}`
   (`accept-ranges: bytes`).
2. **Uploading blobs** — `HEAD /api/blobs/sha256:<d>` dedupe → `POST /api/blobs/…`
   per shard (201). Already-present shards skip instantly.
3. **Assembling** — `POST /api/create {model, files:{…all shards…}}`, relaying Ollama's
   own stream (`parsing GGUF → writing manifest → success`). Model appears in Models tab.

Deploys are **serialized** (one at a time, FIFO queue, in-memory — a server restart
drops waiting jobs) — parallel multi-GB downloads would
thrash net+disk. Digest mismatch on upload → re-download that shard.

---

## 3. Staging store — a disk cache keyed by (repo, quant)

Not scratch — a cache, because it pays off twice:
- **Multi-endpoint**: shards downloaded once push to endpoint B without re-downloading
  50 GB; only the per-endpoint blob upload repeats.
- **Retry/resume**: interrupted download or failed `create` resumes from disk.

Layout mirrors `services/fixed_store.py`:
`instance/hf_staging/<repo>__<quant>/` = the `.gguf` shards + `meta.json`
(repo, quant, shard names/sizes/sha256s, status, endpoints-pushed-to, timestamps,
target model name).

Transient host disk sized to the **full family** (all shards, no 2× — no merge).
Docker: mount a staging volume.

---

## 4. Staged surfaces — rail card + panel

**Stats-rail card** (always visible, in `lh-stats` alongside Running / Total /
Fixed models): a compact "Staged" summary — family count + total GB on disk (+ a
tiny list / "deploying…" indicator when active). Always-on glance value, like the
other rail cards.

**Staged panel** (bottom of the Deploy tab, stacked under Browse) — the full manager.
One row per staged family:
**repo/quant · size on disk · shards · status · pushed-to endpoints · age**,
with **[Deploy to current endpoint]** and **[Prune]**.

- **Manual prune** = delete that family's files.
- **Auto-prune = hard total size cap + LRU**. Keep total staging under
  `HF_STAGING_MAX_GB` (hard cap); when a new family would exceed it, evict
  least-recently-used complete families until it fits (never the one being deployed).
- **UI must state**: pruning staging does NOT remove the model from Ollama — the blobs
  already live in Ollama's store; staging is only the transient download cache.

---

## 5. Backend shape

- `services/hf_hub.py` — HF browse: `search_models(q, cursor)`, `repo_quants(repo)`
  (TTL-cached), quant-family grouping.
- `services/hf_deploy.py` — `deploy_family(...)` (download + blob-upload + create, yields
  progress dicts) and the `DeployQueue` singleton `deploys`.
- `services/staging_store.py` — disk layout, `meta.json`, LRU/size accounting,
  `list_staged()`, `prune(id)`, `auto_prune()`.
- `api.py` — `GET /api/hf/search`, `GET /api/hf/repo/{repo}/quants`,
  `POST /api/hf/deploy`, `GET /api/hf/deploy/status`, `DELETE /api/hf/deploy/queue/{id}`,
  `GET /api/hf/staging`,
  `DELETE /api/hf/staging/{id}`.
- Deploy targets the **currently selected endpoint** (`X-Ollama-Url`); staging is
  endpoint-agnostic, blob upload is per-endpoint.

---

## Open / later
- druids gaps this tab may surface (search-scroller, expandable table row, multi-stage
  progress) → log to `GAPS.md` per the workflow if ≥2 apps would want them.
- Exact tab name ("Deploy" / "Shard"), and whether the Models "Pull" input eventually
  folds in here.

## Quant list: `<druid-popover>` (current, druids 1.0.3)
The quant-count cell is a `<druid-popover placement="left">` whose `slot="trigger"` is a
`<druid-button variant="outline">` ("27 ▾"). The outline variant is the dropdown-trigger
look (base bg, accent border at rest); the popover's own panel supplies the dropdown
surface (dark `--bg-raised`, accent border, `--radius`, shadow) in the **top layer** so
the scrollable results table can't clip it. `placement="left"` opens the panel to the
left of the trigger with **top borders aligned**, flipping right if it won't fit and
clamping to the viewport. Content is a clean aligned table (quant label · size · shard
count). **Deploy = clicking a quant row** → the model-name confirm dialog
(`druids.prompt`) → deploy starts. Clicking the trigger again toggles it closed.

The primitive owns positioning, top-layer placement, light-dismiss (outside-click / Esc),
and same-trigger toggle — so the app carries none of that. What remains app-side:
- **Lazy content.** A search returns many repos; quants (a `blobs=true` call) load only on
  first open. The popover fires `popover-toggle` (`detail.open`); `fillQuantPopover` fetches
  `/hf/repo/{repo}/quants` (cached in `hfQuantsCache`, guarded by a `data-loaded` flag),
  fills `.lh-quant-pop-body`, then calls `popEl.position()` to re-place against the final
  size. The `popover-toggle` listener sits on the **stable `#hf-results-table`**, not the
  tbody — sortable.min.js clone-replaces the tbody on sort, which would drop a tbody-bound
  listener (the popovers themselves move with their rows and survive).
- **Close-on-select.** `selectQuant(event, …)` calls `.hide()` on the owning popover before
  opening the deploy prompt.

History (all dropped): a hand-rolled `popover="manual"` + manual positioning + scroll/
resize/outside-click boilerplate + active-anchor tracking (replaced wholesale by the
primitive); before that an inline clickable badge grid (wasted row width / truncated) and
a modal. See GAPS.md — the popover, outline button, and tooltip gaps are now resolved and
consumed.
