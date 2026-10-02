# Repository Audit: Real-Time Collaborative Sync Engine ("Monach Sync")

Audit date: 2026-09-30. Scope: every tracked file at commit `c98dc6e`. Read-only audit; the only file written is this one.

**Labels.** VERIFIED means I ran it and saw the result, or traced the code path and the outcome is certain. SUSPECTED means the code looks wrong but I could not run it here, usually because it needs Postgres or Redis.

**Environment.** Windows 11 and Python 3.12.1. I made a fresh clone and ran `pip install -r requirements-dev.txt` exactly as the README says, which installed Django 5.1.15. Docker was not running, so Redis and Postgres were unavailable. The Redis-dependent tests were rerun with an in-memory channel layer through a scratch settings override, so a missing broker is not the reason they fail.

---

## Phase 1: What the project is

**Stack.** Django 5.1 + Channels 4 (Daphne ASGI), channels-redis, Celery with a Redis broker, Postgres (SQLite fallback), a vanilla-JS textarea editor, and a hand-written RGA CRDT in both Python (`crdt/`) and JS (`static/js/rga.js`).

**Entry points.**
- `config/asgi.py`: HTTP goes to Django views; WebSocket `/ws/docs/<uuid>/` goes to `documents/consumers.py:DocumentConsumer`.
- `config/celery.py`: the Celery worker, which runs `ai/tasks.py`.
- `loadtest/swarm.py`: the WebSocket load generator.

**Data flow.** The browser diffs the textarea against its local RGA and emits one op per character over the WebSocket. The server then:
1. calls `database_sync_to_async(apply_operation)`;
2. takes a per-process `threading.Lock`, `select_for_update` on the Document row, increments `head_seq` and inserts an `Operation` row;
3. applies the op to a **per-process in-memory** RGA cache;
4. writes a snapshot every 500 ops;
5. broadcasts via `group_send`.

Reconnect uses `{"type":"sync", last_seq, pending}`. For AI requests, the consumer creates an `AIJob`, then `rewrite_task.delay()` runs `AIPeer.execute_task`, which buffers the LLM output, diffs it, and applies the resulting ops.

**External services.** Redis (channels + Celery), Postgres, and an OpenAI-compatible HTTP API. The API is optional; with no key it silently falls back to `FakeLLMClient`.

**Config.** Settings come from `os.environ` with insecure fallbacks. There is no `.env` handling, no `CACHES`, and no `LOGGING`.

### Claims vs. reality
| README claims | Code reality |
|---|---|
| Google-Docs-style live collaborative editing | The server **deadlocks on the first edit** (C1). No edit can be persisted. |
| Horizontal scale on a 2-node Daphne cluster | Each process holds its own RGA cache, so a second node serves stale state and writes corrupt snapshots (C5). |
| One-command `docker compose up` | Nothing runs migrations, and no migrations exist, so the app's tables are never created (C2). |
| 462 ops/s, 38.6 ms p50 at 200 clients | Impossible to have produced with this code: the write path deadlocks and `websockets` (the load test's client library) is not a dependency (C6). |
| 96.7% eval pass rate over 30 cases | The runner prints **100%** against a canned fake LLM, and it counts length failures as passes (C6). |
| 10,000+ permutations proving convergence | 24 permutations + 500 random runs + 150 Hypothesis examples. |
| AI co-author streams edits as a CRDT peer | The AI path raises `SynchronousOnlyOperation`. Even if fixed, output is buffered and applied at the end, not streamed (H1, M9). |
| "What changed while you were away" summaries, semantic search | The tasks exist but nothing calls them. Embeddings are `ord(c) % 50`. |
| Owner/editor/viewer permissions | Anonymous users and any non-collaborator get `editor` on every document (C3). |

**Bottom line.** The CRDT library in `crdt/` is real and well tested. Everything built on top of it is non-functional or insecure.

---

## Phase 2: Running it

| Step | Result | Status |
|---|---|---|
| `pip install -r requirements-dev.txt` (clean venv) | OK. Note that the committed-adjacent `.venv` has **Django 6.1.1**, which violates the `<5.2` pin, so the author's own environment doesn't match the requirements. | VERIFIED |
| `ruff check .` / `ruff format --check .` | Pass | VERIFIED |
| `mypy crdt/` (what CI runs) | Pass | VERIFIED |
| `mypy crdt documents ai config` | 43 errors, all missing stubs (no `django-stubs` / `types-channels`). Only `crdt/` is actually type-checked. | VERIFIED |
| `python manage.py migrate` on a fresh DB | Succeeds, but creates **zero** `documents_*` tables: `documents/` has no `migrations/` package. `makemigrations --check` reports "No changes" because Django ignores apps without a migrations module. | VERIFIED |
| `pytest tests/crdt tests/vectors` | 34 passed, 94% coverage of `crdt/` | VERIFIED |
| `node --test tests/vectors/test_rga_js.mjs` | 3 passed | VERIFIED |
| `pytest tests/test_services.py` | **Hangs forever.** A faulthandler dump shows `services.py:105 apply_operation` blocked in `services.py:37 get_or_load_document_rga`. | VERIFIED |
| `pytest tests/test_history.py` | Hangs (same deadlock) | VERIFIED |
| `pytest tests/test_consumer.py` (in-memory layer) | The connect/init test passes. The op-broadcast test hangs on the same deadlock. | VERIFIED |
| `pytest tests/test_reconnect.py` (in-memory layer) | Hangs; faulthandler shows `services.py:37` ← `:105` | VERIFIED |
| `pytest tests/test_ai_peer.py` | Fails with `SynchronousOnlyOperation`: the test itself calls the sync ORM from an async test (`tests/test_ai_peer.py:55`) | VERIFIED |
| `python -m ai.evals.runner` | `passed: 30, pass_rate_pct: 100.0, avg_latency_ms: 0.01` | VERIFIED |
| CI (`.github/workflows/ci.yml`) | No `services:` block (no Redis/Postgres) and no test timeout. The integration tests cannot pass there. The "CI passing" badge can only be green if the job hangs until it times out or is not being run. | VERIFIED (config), SUSPECTED (actual badge state) |

**Conclusion.** The project cannot run from a clean clone following the README. Only the pure CRDT library works.

---

## Phase 4.1: Verdict

**Prototype, not demo-ready. Score: 2.5/10.**

The `crdt/` package is a correct, readable, well-tested RGA. It has property tests, shared JS/Python test vectors and strict mypy, and it is the one piece I'd defend in an interview. Everything the README sells on top of it is broken:
- the single write path deadlocks on its first call;
- the tables are never created;
- the AI peer throws on every job;
- the multi-node deployment would silently corrupt persisted snapshots;
- authorization is effectively absent, since anyone, including anonymous users, gets `editor` on every document.

Most damaging for a portfolio project: the benchmark tables, the eval pass rate and the "10,000+ permutations" claim are not reproducible from this code, and several are contradicted by it. A senior reviewer who runs the tests will find the deadlock within five minutes, and after that every number in the README reads as fabricated. Fix the correctness bugs and remove the unbacked numbers before this goes anywhere near a resume.

---

## Phase 4.2: Findings

Sorted by severity. Effort: S < 2h, M < 1 day, L > 1 day.

| ID | Sev | Category | File:Line | Problem | Fix | Effort | Label |
|---|---|---|---|---|---|---|---|
| C1 | Critical | Correctness | `documents/services.py:77-78,105` → `:36-37` | `apply_operation` holds `doc_lock` (a non-reentrant `threading.Lock`) and then calls `get_or_load_document_rga`, which does `with doc_lock:` again. Every op, revert, AI edit and sync deadlocks the thread. Because `database_sync_to_async` is thread-sensitive, this freezes **all** DB work in that Daphne process. | Split out an internal `_load_rga_locked(doc_id)` that assumes the lock is held, and call it from `apply_operation`. (Using `threading.RLock` is a quick patch, not the fix.) Add a regression test with a timeout. | S | VERIFIED (faulthandler stack) |
| C2 | Critical | DevEx/Correctness | `documents/` (no `migrations/`); `docker-compose.yml:46,72` | There are no migrations, so `migrate` creates no app tables. Compose starts Daphne without running `migrate` at all. The README Quickstart fails on first page load. Tests pass only because pytest-django syncdb's apps that have no migrations. | Run `makemigrations documents` and commit. Add a `migrate` step (entrypoint or one-shot compose service) and `makemigrations --check` in CI. | S | VERIFIED |
| C3 | Critical | Security (authz) | `documents/consumers.py:448-450,459` | Unauthenticated WebSocket users get `(True, "editor")`, and **any** logged-in non-collaborator also gets `"editor"` ("Default open access for testing"). Anyone who knows or guesses a doc UUID can read the full snapshot and write to it. The HTML view computes `viewer` (`documents/views.py:70-76`), so the UI and the server disagree. | Reject anonymous users (`close(4001)`). Return `(False, "")` when the user is neither owner nor collaborator. Put this in one `get_role(user, doc)` function used by both HTTP and WS. | S | VERIFIED (trace) |
| C4 | Critical | Security (IDOR) | `documents/views_history.py:31,70,99`; `documents/views.py:56-81` | The history, state-at-seq and **revert** endpoints only check `LoginRequiredMixin`, and then `get_object_or_404(Document, id=doc_id)`. Any registered user can dump any document's full edit history (every character, with usernames) and revert anyone's document. Registration is open (`RegisterView`). | Scope the lookups to owner/collaborator and require `owner` or `editor` for revert. Add tests for the 403 cases. | S | VERIFIED (trace) |
| C5 | Critical | Architecture/Data integrity | `documents/services.py:19-20,38-39,105-114`; `docker-compose.yml:43-93` | `_doc_cache` is process-local. With `web_1`, `web_2` and the Celery worker, each process only applies its own ops to its cached RGA. (1) The `init` payload (`consumers.py:461-470`) serves stale text. (2) The snapshot at `server_seq % 500 == 0` is taken **from the stale local replica**. Later loads start from that snapshot and replay only `seq > N`, so ops applied on other nodes before N are **permanently lost**. | Short term: before using the cache, replay `Operation` rows with `seq > rga_seq` (track the last-applied `server_seq` per cached RGA). Build snapshots from DB replay, never from the cache. Long term: route each document to one owner process (sticky/consistent hashing), or make the cache a pure read-through of DB state. | M | VERIFIED (trace) |
| C6 | Critical | Integrity / Interviewer | `README.md:25-37,98-99,154-160`; `docs/BENCHMARKS.md:9-17,43-50`; `ai/evals/runner.py:78-82` | Performance and eval numbers are unreproducible or false. (a) The write path deadlocks (C1), so no load test could have reached 462 ops/s. (b) `loadtest/swarm.py:38` imports `websockets`, which is in no requirements file; failures are swallowed at `:45-47`. (c) The runner outputs 100%, not the claimed 96.7%, and `passed += 1` sits in **both** branches of the length check (lines 79-82), against a canned `FakeLLMClient`. (d) "10,000+ permutations" is really 24 + 500 + 150. (e) The "14.2 ms per token chunk" and "182 ms summary" figures have no code path that measures them. | Delete every number you can't regenerate with a committed script plus a committed raw output file. Fix the evals runner to fail on failures, and make it require a real client for any published number. | S | VERIFIED |
| H1 | High | Correctness | `ai/agent_peer.py:154-167,72,91,110,308`; `ai/tasks.py:49-57` | `execute_task` is `async def` but calls the sync ORM (`AIJob.objects.filter(...).first()`, `job.save()`, `check_cancelled()`) and `async_to_sync(...)` while `loop.run_until_complete` is running. Django raises `SynchronousOnlyOperation`, and `async_to_sync` raises inside a running loop. Every rewrite/grammar/shorten/continue job fails. | Make `execute_task` synchronous: consume the stream with `asyncio.run` in a tiny helper that only does HTTP, and do all ORM/channel-layer work outside the loop. Or wrap each ORM call in `sync_to_async`. | M | VERIFIED (trace; same exception reproduced in test run) |
| H2 | High | Security (IDOR) | `documents/consumers.py:357-359,414-417,420-421,439-440` | `suggestion_accept`, `suggestion_reject` and `ai_cancel` look up objects by ID with **no document filter**, and reject/cancel have no role check. A viewer, or a client on any doc, can accept, reject or cancel other documents' suggestions and jobs. Accepting also applies the ops to `self.doc_id` using another doc's anchors. | Filter by `document_id=self.doc_id` and check the role on every handler. | S | VERIFIED (trace) |
| H3 | High | Security/Correctness | `crdt/ops.py:101-126`; `documents/consumers.py:141-152` | Client ops are trusted wholesale. `site_id`, `char_id.site_id`, `lamport` and `char_id.clock` are client-chosen and never checked for consistency or bound to the user. A client can (a) spoof another site's attribution; (b) send `lamport=2**70`, which overflows `BigIntegerField` and raises an unhandled exception that kills the socket; (c) send `site_id` longer than 64 chars, which raises `DataError` on Postgres; (d) jump everyone's Lamport clock to near-max. | Add server-side schema validation: `char_id.site_id == site_id`, a site-id prefix bound to the session user, `0 < lamport < 2**53`, length limits, and `char_id.clock == lamport` for inserts. Wrap `apply_operation` in try/except that returns an `error` message. | M | VERIFIED (no validation); SUSPECTED (PG crash, not run on PG) |
| H4 | High | Reliability/DoS | `crdt/rga.py:71,174,183,351`; `documents/consumers.py:68` | `_pending_ops` is unbounded: inserts whose parent never arrives are persisted (`services.py:93`), buffered forever, and replayed into every future load. `applied_op_ids` (every op ID ever) goes into each snapshot **and is sent to every client in `init`**, so connect payload and snapshot size grow O(total ops ever), not O(document). | Reject ops whose parent/target is unknown to the server (the server is authoritative, so it never needs to buffer). Drop `applied_op_ids` from snapshots; dedupe via the DB unique constraint. Send clients nodes + `head_seq` only. | M | VERIFIED (trace) |
| H5 | High | Performance/Correctness | `documents/consumers.py:176,194-204`; `documents/services.py:140-145` | On every reconnect, `sync` re-broadcasts **all** missed ops to the whole room, not just the newly applied pending ones. With `last_seq=0` that is the entire history: unpaginated, loaded into memory, and fanned out to every peer. `int(content.get("last_seq"))` raises on bad input. | Broadcast only the ops that `apply_operation` newly applied. Cap or paginate catch-up (for large gaps, send a snapshot instead). Validate `last_seq`. | S | VERIFIED (trace) |
| H6 | High | Security (config) | `config/settings.py:12-18`; `config/asgi.py:24`; `docker-compose.yml:50-51` | Hardcoded fallback `SECRET_KEY`, `DEBUG=True` by default, and `ALLOWED_HOSTS="*"`. There is no `AllowedHostsOriginValidator` on the WS router, so a malicious site can open an authenticated socket with the victim's session cookie (cross-site WebSocket hijacking). No secure-cookie or HSTS settings. | Fail at startup if `SECRET_KEY` is unset and `DEBUG` is off. Default `DEBUG=False`. Wrap the WS router in `AllowedHostsOriginValidator`. Add the `SECURE_*`, `CSRF_TRUSTED_ORIGINS` and `SESSION_COOKIE_SECURE` settings. | S | VERIFIED (config) |
| H7 | High | Reliability | `documents/consumers.py:289-298` | `rewrite_task.delay()` is a synchronous broker publish inside an async consumer. When Redis is slow or down, kombu's publish retries block the event loop for **every** socket on that process. The fallback `asyncio.create_task(database_sync_to_async(task))` holds no reference and runs the whole LLM call on the thread-sensitive executor, blocking all DB work in the process. | Use `await sync_to_async(task.apply_async, thread_sensitive=False)(..., retry=False)`. Report failure to the client instead of silently running in-process. | S | SUSPECTED |
| H8 | High | Performance | `crdt/rga.py:259-327`; `static/js/editor.js:249-291`; `documents/consumers.py:465-469` | Every positional operation is O(n) over all nodes, **tombstones included**: `_find_visible_node_at_index`, `text()`, `visible_len()`, `pos_of_char_id`. The client calls `rga.text()` and `visibleLen()` per keystroke and per remote op. Pasting k chars into an n-node doc costs O(k·n), and tombstones are never collected. The README's "O(1) hash indexing" only covers lookup by ID. | Add an order-statistic tree or skip list (or chunked blocks) for index↔node mapping. Cache `text` incrementally. Add tombstone GC once all sites are past a stable seq. | L | VERIFIED (trace) |
| H9 | High | Correctness | `static/js/editor.js:274,286-291`; `crdt/ops.py:42` | The textarea diff iterates UTF-16 code units, so an emoji becomes two ops carrying lone surrogates. Python accepts `len(char)==1` surrogates, but they can't be UTF-8-encoded into Postgres JSONB. Expect an insert failure and a dead socket, or divergent text between clients. | Iterate by code point (`Array.from(str)`) or grapheme. On the server, reject lone surrogates. Add emoji to the shared test vectors. | S | SUSPECTED |
| M1 | Medium | Reliability | `documents/views_history.py:124-137` | Revert applies N compensating ops as N separate transactions plus N `group_send` calls inside a sync request. A mid-way failure leaves the document half-reverted, and a large revert holds the request for seconds. | Apply all ops in one transaction with one seq range, then broadcast a single batch message. | M | VERIFIED (trace) |
| M2 | Medium | Performance | `ai/agent_peer.py:202,300,303-316` | `check_cancelled()` runs a DB query per streamed chunk and per generated op. Each op is its own transaction plus `group_send`. A 500-char rewrite costs about 1,500 round trips. | Check cancellation every N chunks or on a timer. Batch the ops into one transaction and one broadcast. | S | VERIFIED (trace) |
| M3 | Medium | Correctness | `static/js/sync.js:115-121,139-148` | `onerror` and `onclose` both call `_handleDisconnect`, and a WebSocket error is always followed by close. Each failure schedules two reconnects, which can leave duplicate live sockets. After 50 attempts the client stops silently, with no UI signal. | Reconnect only from `onclose`, guard with a single pending timer, and never stop retrying (cap the delay instead). | S | VERIFIED (trace) |
| M4 | Medium | Performance | `static/js/sync.js:30-35,37-42` | Every keystroke reads, parses and rewrites the whole pending-ops array in `localStorage`, which is O(n²) while offline and hits the ~5 MB quota quickly (each op is ~150 bytes of JSON). The file header claims IndexedDB; it's localStorage only. | Keep an in-memory queue and flush on a debounce, or use IndexedDB for real. | S | VERIFIED (trace) |
| M5 | Medium | Security | `ai/guards.py:97-162`; `config/settings.py` (no `CACHES`) | "Redis cache rate limiting" is actually Django's default **LocMemCache**: per-process, reset on restart, and multiplied by the number of workers. The get-then-set is non-atomic. The token budget charges a fixed 1000 tokens per request instead of real usage. | Configure `CACHES` to Redis and use `INCR`/`EXPIRE` (or a Lua sliding window). Charge actual usage after completion. | S | VERIFIED |
| M6 | Medium | Security | `documents/consumers.py:400-402` | Anonymous AI jobs are attributed to `User.objects.first()`, typically the superuser, or the code creates `anonymous_ai_user` on the fly. | Once C3 is fixed anonymous users can't get here, so delete the branch. | S | VERIFIED |
| M7 | Medium | Security | `ai/tasks.py:159-171`; `ai/guards.py:15-20,68` | `suggestion_task` skips `sanitize_document_text` and `validate_input_bounds` and has no try/except, so on failure the job stays "queued" forever. The injection "defense" consists of 4 regexes applied only to the instruction, never to document text. | Route all AI kinds through one guarded pipeline with error handling. Describe the regexes honestly as a heuristic, not "enterprise guardrails". | S | VERIFIED |
| M8 | Medium | Correctness | `documents/models.py:73-74`; `documents/services.py:83-85` | `op_id` is globally unique, but the idempotency check isn't scoped to the document. Replaying an op_id on a different doc returns an `ack` with **the other document's** seq, and the op is silently dropped. | Use `unique_together=(document, op_id)` and filter by document. | S | VERIFIED (trace) |
| M9 | Medium | Interviewer | `ai/agent_peer.py:196-217`; `README.md:87-88` | "Tokens streamed from the LLM are converted into CRDT ops" is not what happens. All chunks are accumulated, then diffed once and applied at the end. There is no concurrent streaming of AI edits. | Either implement incremental application (apply per sentence, anchored on the last inserted CharId) or reword the README. | M | VERIFIED |
| M10 | Medium | Observability | `documents/metrics.py:1-105`; `config/urls.py:12` | `prometheus_client` isn't a dependency, so `/metrics` returns 501. Even if installed, no code anywhere calls `.inc()`/`.observe()` on any metric. The endpoint is also unauthenticated. There is no `LOGGING` config, and failed broadcasts are logged at `debug` (`agent_peer.py:93,112,318`). | Add the dependency, instrument `apply_operation` / connect / disconnect, restrict `/metrics` to the internal network, and configure structured logging. | M | VERIFIED |
| M11 | Medium | Testing | `tests/test_reconnect.py:246-277`; `tests/test_ai_peer.py:55`; `.github/workflows/ci.yml:9-46` | The integration tests never cross the 500-op snapshot boundary, never use two processes or two caches (C5 is invisible to them), and never test authz. `test_ai_peer` calls the sync ORM from an async test and errors before exercising the code. CI has no Redis/Postgres services and no timeouts, so a hang runs until the 6h job limit. | Add a Postgres+Redis `services:` block and `pytest-timeout`. Add tests for snapshot + replay, two-cache consistency, 403s, and C1 as a regression. | M | VERIFIED |
| M12 | Medium | DevEx/Ops | `Dockerfile:16-18,25`; `docker-compose.yml:47-48,50` | The prod image installs **dev** deps and runs as root. Compose bind-mounts `.:/app` over the image, sets `DEBUG=True`, and has no web healthchecks, `migrate` step or `collectstatic`. Static files are served by Django through nginx. | Use a multi-stage build with runtime deps only, a non-root user, and an entrypoint that runs `migrate` + `collectstatic`. Have nginx serve `/static/`. Move the dev mount to `docker-compose.override.yml`. | M | VERIFIED |
| M13 | Medium | Performance | `documents/views_history.py:34-50` | The history API serializes the entire op log (one row per character) unpaginated. | Paginate by seq range and group server-side. | S | VERIFIED |
| M14 | Medium | Architecture | `documents/services.py:182-231`; `ai/agent_peer.py:259-296` | The "diff text → CRDT ops" logic is copy-pasted between revert and AI. Both deep-copy the whole RGA via `from_dict(to_dict())` per request. | Extract `ops_for_replacement(rga, start, end, new_text, site_id)` into `crdt/` and test it once. | S | VERIFIED |
| L1 | Low | Correctness | `static/js/rga.js:80` vs `crdt/ids.py:52` | JS `split('@')` truncates site IDs containing `@`; Python uses `split('@', 1)`. The two ports diverge. | Use `indexOf`/`slice` in JS and add a vector case. | S | VERIFIED (trace) |
| L2 | Low | Correctness | `crdt/rga.py:193-195` + `documents/services.py:87-102` | An insert reusing an existing `char_id` under a new `op_id` still gets a seq, is persisted and is broadcast, but it's a no-op in the RGA. The log fills with phantom ops. | Reject it in validation (H3). | S | VERIFIED (trace) |
| L3 | Low | UX/Claims | `static/js/presence.js:405-444`; `documents/consumers.py:221-228` | Remote cursors are never rendered: presence only draws avatars. Collaborators are keyed by username, so two tabs of the same user collapse into one. `color` and `cursor_*` are relayed unvalidated. | Render cursors from `cursor_anchor`, key by channel, and validate fields. | M | VERIFIED |
| L4 | Low | Code quality | `ai/__init__.py:5`; `config/celery.py:20-22`; repo root | `default_app_config` has been deprecated since Django 3.2. `debug_task` is dead code. Planning scratch files are committed (`plan.txt`, `project.txt`, `Implemetaiont_plan.md` (typo in the name)). `powershell.bat` is git-ignored but present. The history is six commits all named "Initial commit". | Delete them. Write real commit messages going forward. | S | VERIFIED |
| L5 | Low | Dependencies | `requirements.txt` / `pyproject.toml` / `uv.lock` (staged) | Dependencies are declared in three places. `pgvector` and `msgpack` are unused. `websockets` is used but missing. `factory-boy` is unused. | Use one source (pyproject + uv.lock) and generate requirements from it. | S | VERIFIED |

---

## Phase 4.3: Top 10 fixes (highest impact per effort first)

1. **Fix the deadlock (C1).** In `services.py`, move the body of `get_or_load_document_rga` into `_load_rga_unlocked(doc_id)`. The public function becomes `with lock: return _load_rga_unlocked(doc_id)`, and `apply_operation` calls `_load_rga_unlocked` directly. Add `pytest-timeout` (`timeout = 30`) so a regression fails instead of hanging.
2. **Commit migrations and run them (C2).** Run `python manage.py makemigrations documents`, add `python manage.py migrate --noinput` to an entrypoint, and add `makemigrations --check --dry-run` to CI.
3. **Real authorization in one place (C3, C4, H2).** Create `documents/permissions.py::get_role(user, doc) -> str | None` and use it from the consumer, the editor view and all three history views. Reject anonymous users. Scope every by-ID lookup (`Suggestion`, `AIJob`) to `document_id=self.doc_id`. Write six small tests for the 403 cases.
4. **Remove unbacked numbers (C6).** Delete the benchmark and eval tables from README and BENCHMARKS until a committed script regenerates them. Fix `runner.py:79-82` to count failures. Replace "10,000+ permutations" with what the tests actually do.
5. **Make the cache coherent (C5).** Store `last_seq` on each cached RGA. In `apply_operation`, after `select_for_update`, replay `Operation` rows with `server_seq > rga.last_seq` before applying the new op. Build snapshots in a Celery task from DB replay, never from the in-process cache.
6. **Validate incoming ops (H3, H9, L2).** Enforce `char_id.site_id == site_id`, a site ID bound to the user, bounded lamport, a known parent/target, single code point, and no lone surrogates. Put this in a `validate_client_op(op, user, rga)` function, and wrap the handler in try/except that sends `{"type":"error"}` instead of crashing the socket.
7. **Fix the AI path (H1, H7, M2).** Make the Celery task fully synchronous around a single `asyncio.run(collect_stream())`. Publish with `sync_to_async(apply_async)`. Batch the generated ops into one transaction and one broadcast.
8. **Stop the payload blow-up (H4, H5).** Drop `applied_op_ids` from snapshots and `init`. Broadcast only newly applied pending ops. Paginate or cap catch-up and fall back to a snapshot.
9. **Secure defaults (H6).** Require `SECRET_KEY`, default `DEBUG=False`, derive `ALLOWED_HOSTS` from env, use `AllowedHostsOriginValidator(AuthMiddlewareStack(...))`, and configure Redis `CACHES`, which also fixes M5.
10. **CI that proves it works (M11).** Add Postgres and Redis service containers, `pytest-timeout`, the integration suite, and one two-process test: two separate `_doc_cache` instances that must converge across a snapshot boundary.

---

## Phase 4.4: Cut list

- **`DocChunk` model + `semantic_embed_task`** (`documents/models.py:192-207`, `ai/tasks.py:206-240`): the embeddings are fake (`ord(c) % 50`), nothing calls the task, and nothing ever reads the model.
- **`pgvector` and `msgpack` dependencies**: never imported.
- **`summarize_missed_edits_task`** (`ai/tasks.py:60-140`): nothing invokes it and there is no ">20 missed ops" trigger anywhere. Either wire it up and test it, or delete it along with its README section.
- **`documents/metrics.py` NoOp fallback shim**: either make Prometheus a real dependency and instrument code, or delete the file and the `/metrics` route.
- **`LamportClock` lock** (`crdt/clock.py:23`): the RGA isn't thread-safe anyway, so the lock suggests a guarantee that doesn't exist. Document the RGA as single-threaded instead.
- **The "Suggestion" / "ask" `AIJob.KIND_CHOICES` entries** with no implementation (`ask` has no handler).
- **Duplicated diff-to-ops code**: keep one copy (M14).
- **`plan.txt`, `project.txt`, `Implemetaiont_plan.md`, `powershell.bat`, `config/celery.py:debug_task`.**
- **README "Resume Ready Bullets" section.** A README that ships its own resume bullets with unverified numbers is the first thing an interviewer will poke at.

---

## Phase 4.5: 7-day improvement plan

| Day | Work | Exit criteria |
|---|---|---|
| 1 | Fix C1 and C2. Add `pytest-timeout`. Commit migrations and a `migrate` entrypoint. Fix `test_ai_peer.py:55`. | All existing tests pass locally against Postgres + Redis. `docker compose up` gives a working editor. |
| 2 | Implement `get_role` and fix C3, C4, H2 and M6. Write the authz test file. | An anonymous or non-collaborator socket is closed. History and revert return 403. Viewer writes are rejected. |
| 3 | Fix C5 (seq-tracked cache + DB-replay snapshots) and M8. Add the two-cache convergence test across a snapshot boundary. | The test fails before the fix and passes after. |
| 4 | Op validation (H3, H9, L2) and error handling in the consumer. Fix H4 and H5 (no buffering on the server, slim `init`, targeted rebroadcast). | Fuzz test: 1k random malformed ops never kill the socket. `init` size is O(doc). |
| 5 | Fix the AI path (H1, H7, M2, M7) and the evals runner. Configure the Redis cache (M5). | An AI rewrite end-to-end test passes with `FakeLLMClient`. The runner reports real failures. |
| 6 | Client fixes (M3, M4, emoji, L1). Harden Docker and compose (M12) and settings (H6). Update CI with services (M11). | CI is green with integration tests. The image runs as non-root with `DEBUG=False`. |
| 7 | Add `websockets` as a dependency and run `loadtest/swarm.py` for real against compose. Commit the script output as `docs/bench/<date>.json`. Rewrite README and BENCHMARKS **only** from that output. Apply the cut list. | Every number in the README links to a committed raw result. |

---

## Phase 4.6: Missing pieces for a production version

- **Sharing/ACL management**: there is no UI or API to add collaborators (admin only), no invitations and no link sharing.
- **Document ownership model for scale-out**: a per-document authoritative actor (sticky routing or a shard map) instead of N divergent in-process caches.
- **Compaction**: tombstone GC, op-log truncation behind snapshots, and snapshot retention.
- **Efficient sequence structure**: a tree or block-based RGA with O(log n) index mapping. Batched ops (run-length inserts for paste) instead of one op per character.
- **Protocol versioning and a schema** for WebSocket messages (pydantic or similar), with explicit error codes.
- **Backpressure and limits**: per-connection op rate limits, max message size, max doc size.
- **Observability**: structured logs with doc/user/seq, real metrics, tracing on op apply, and health/readiness endpoints for web and worker.
- **Security baseline**: origin validation, CSP for the editor page, secure cookies, audit log for reverts, and a dependency scanner (pip-audit) in CI.
- **Data lifecycle**: backups, soft-delete for documents (delete currently cascades the entire op history instantly), export.
- **Rich text**: the editor is a plain `<textarea>` that resets `.value` on every remote op, which destroys the selection range, IME composition and undo history.
- **Load test that asserts**: a swarm run in CI (small scale) that fails on divergence, not one that prints a table.

---

## Interviewer lens: the first five things a senior engineer would question

1. **"Run the tests."** `test_services.py` hangs. That takes about 5 minutes to find, and it discredits every number in the README.
2. **"How does node 2 know about node 1's edits?"** It doesn't, and the snapshots persist the divergence (C5). This directly contradicts the "horizontal scale" headline.
3. **"Who can edit this document?"** Everyone, including anonymous users (C3), and any logged-in user can revert any document (C4).
4. **"Show me the command that produced 462.1 ops/s."** The load test's WebSocket library isn't installed, and the eval runner's pass logic is `passed += 1` in both branches.
5. **"What's the complexity of a keystroke on a 100k-character document with a year of tombstones?"** O(n) several times per keystroke on client and server, and the full op-ID set is shipped to every client on connect (H4, H8).

What would survive the interview: the `crdt/` package, the shared JSON test vectors that check the Python and JS ports against each other, and the idempotent `op_id` + `server_seq` catch-up protocol design. Lead with those, and be honest about everything else.
