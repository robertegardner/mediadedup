# Project history

A narrative log of major decisions, bugs that bit hard, and lessons learned
across the development of mediadedup. Read this if you're wondering "why is
this code shaped like this?" — usually there's a story.

The intended audience is future-you (the operator), and any AI assistant
that needs context beyond what's in CLAUDE.md.

---

## Origin and goals

Built to dedupe a personal video and music library that had accumulated
duplicates across multiple sources over years of automated downloading. The
operator wanted a self-hosted tool that:

- Worked on a Docker stack on an Ubuntu VM, not bare-metal Python
- Used the local NVIDIA GPU (RTX 4060 in production) for video fingerprinting
- Was idempotent and resumable — long runs that get interrupted should not
  start over
- Used MOVED-to-trash semantics, never `rm` — recovery from bad calls is one
  `mv` away
- Had a web UI for review, not just CLI tools

The first iteration was simple: scanner walks files, workers fingerprint,
matcher clusters. The complexity grew as production realities revealed
themselves.

---

## Architecture evolution

### v1: Single shared bind mount, one source

Initial layout: `VIDEO_PATH` and `MUSIC_PATH` env vars bound separate host
paths into the container at `/media/video` and `/media/music`. No `sources`
table; everything was implied by the bind mount layout.

### v2: Multi-source architecture

Adding new shares required edits to `docker-compose.yml` and a restart. Too
clunky for ongoing use. Added a `sources` table:

- `id`, `name`, `path` (container-side), `media_type`, `priority`, `enabled`
- Files got a `source_id` foreign key
- `MEDIA_PATH` env var binds the host parent dir; everything under it is
  available, sources point to whichever subdirs the operator configures

This let the operator add a new share through the dashboard without
restarting anything. Source priority became the primary keeper-selection
tiebreaker: higher-priority source's copy wins.

### v3: Run orchestration and scheduling

Adding scheduler.py (APScheduler) for nightly runs, orchestrator.py to track
runs in `orchestrator_runs`, and reaper.py to reset stuck rows. The
orchestrator uses an in-process `threading.Lock` for mutual exclusion plus
a DB row per run for visibility.

This is what eventually became the source of the "orphan run" problem when
background threads crashed without updating their tracking rows.

### v4: Filename matcher

Added when the operator pointed out that most dupes in his library were
re-downloads with near-identical filenames — same studio, same date, same
actor, different release group. A deterministic normalizer + token-set
jaccard caught ~1,700 dupes covering 1.7 TB in the first run. Implementation
in `app/filename_match.py`.

### v5: LLM matcher

Added when filename matcher caught the easy cases. Idea: use Ollama on a
separate GPU host to cluster the residual cases that the deterministic
normalizer couldn't. Implementation in `app/llm_match.py`. Configurable
via DB-backed settings table.

Outcome: disappointing. See "Failed approaches" below.

---

## Bugs that bit hard

### The path migration cascade

When transitioning from v1's separate-bind layout to v2's MEDIA_PATH layout,
the operator had ~26k file rows with paths starting `/media/video/...` and
~145k with `/media/music/...`. After the migration, those paths no longer
existed inside the container — the new paths were `/media/whisparr/...` and
`/media/music/...`.

The scanner re-discovered files at the new paths and inserted new rows. So
the DB now had TWO rows per file: one with the stale path (which would fail
on disk access) and one with the current path.

Auto-delete attempts hit `PermissionError` because the stale rows had
unreachable paths. The system looked broken.

Fix: a SQL pass to (a) DELETE old rows that had new-path counterparts, and
(b) UPDATE old rows that didn't, replacing the prefix. Then re-resolve
source_ids for everyone.

**Lesson**: when changing path conventions, plan the data migration as
explicitly as the schema migration.

### The mass-missing flap (TWICE)

Symptom: after a restart, every file flipped from `done` to `missing`. The
dashboard showed 0 files monitored. Workers had nothing to do.

Cause: scanner runs at startup. The CIFS mount via autofs wasn't ready when
scanner fired. Every `os.path.exists(row['path'])` returned False because
the mount was empty. Scanner happily flipped all 150k rows to `missing` in
one transaction.

Recovery: restore based on `sha256 IS NOT NULL` (fingerprinted rows go back
to `done`, others to `pending`). Took 60 seconds of SQL. But the
psychological impact was bad — the dashboard saying "0 files" feels like
data loss even when it isn't.

This happened TWICE before getting fixed. The first time was attributed to
"the mount glitched, weird." The second time made it clear this was a
recurring class of bug.

The fix added three guards to `mark_missing_for_source`:
1. 60-second startup grace (refuse to mark anything missing in the first
   minute of process life — let autofs settle)
2. Path-alive check (refuse if the source's top-level directory lists no
   entries — almost certainly a broken mount)
3. 10% percentage cap (refuse if it would flip >10% of the source's
   `done` files — that's never a legitimate normal-operation event)

**Lesson**: when a destructive operation can run automatically based on
external state (file existence on a network mount), it MUST validate the
external state is healthy before acting. "The file isn't there" is not
the same as "the file shouldn't be there."

### The matcher silent crash

Symptom: clicked "Run matcher", UI showed "running" for hours, no progress
visible, eventually realized no matcher container was running, web logs
showed nothing.

Cause: matcher loaded all phashes + chromaprints into memory for pairwise
comparison. With heavy worker activity and a large library, memory pressure
spiked. The matcher's host thread got SIGKILLed by the kernel OOM-killer.
Background daemon threads dying in Python this way leave no traceback.

Recovery: clear the orphan `orchestrator_runs` row, restart workers.

**Lessons:**
1. Don't run matcher during heavy worker activity.
2. Background work needs visible error reporting. The reaper thread we have
   for stuck `processing` rows in the `files` table needs a sibling for
   stuck rows in `orchestrator_runs`. (Still TODO.)
3. The matcher's O(n²) memory model is a latent timebomb. Should be chunked.

### Filename matcher phase 2 hang

Symptom: filename matcher started, completed phase 1 quickly with N groups,
then never returned. Ctrl-C was required to recover.

Cause: phase 2 (token-set jaccard) did all-pairs comparison within each
directory bucket. Most files in the operator's library lived under
`/media/whisparr/` root — single bucket of 10k+ files → 50M+ pair comparisons.

The matcher had worked fine in earlier runs because the first auto-delete
pass had already removed most of those files. With fresh `pending` files
re-populating the bucket, the bug appeared.

Fix: sub-bucket large buckets by first alphabetic content token (usually
studio name), cap sub-bucket size at 400, hard pair-count budget of 2M as
last-resort circuit breaker.

**Lesson**: O(n²) algorithms in matcher code need explicit bucketing AND
size caps AND a global budget. Three layers of defense for what should be
an obvious red flag.

### The Web container template-cache rebuild dance

Symptom: changed `app/templates/index.html`, copied to the container,
restarted web, no change visible. Hard refresh still showed old template.

Cause: Jinja2 templates are baked into the Docker image at build time. A
`cp` into a running container doesn't change what `image:latest` contains.
A `docker compose up -d --force-recreate web` starts a new container from
the SAME image. So template changes require `docker compose build web`
THEN recreate.

The operator hit this several times before internalizing the build step.

**Lesson**: any change to a file under `app/` requires `docker compose build`,
not just restart. Mention this explicitly in deployment instructions.

### The set_url race in the LLM settings panel

Symptom: typing a new Ollama URL and clicking "Test connection" tested the
OLD URL (the one saved in the DB), not the new one in the form field.

Cause: the test endpoint had no way to override the stored setting. The UI
just probed whatever was in the DB. So testing a URL required saving it
first, but saving a URL required successfully testing it first — chicken
and egg.

Fix: test endpoint takes an optional `?url=` parameter that overrides the
stored setting. UI passes the current form value.

**Lesson**: UI controls that allow testing config before saving need
backend endpoints that accept the unsaved values explicitly.

### The Python-format-spec in a JS template literal

Symptom: dropdowns didn't populate, buttons didn't work, form fell back to
default GET submission. No errors visible until checking the browser console.

Cause: line 953 in index.html had `${c.model || '?':12s}` inside a JS
template literal. The `:12s` is Python's str.format syntax — pad to 12 chars.
JS doesn't have that; the parser hit `:` after `'?'` and threw
"Missing } in template expression." That killed the entire `<script>` block,
preventing the rest of the page's JavaScript from running.

Fix: replaced with `(c.model || '?').padEnd(12)`, the JS equivalent.

**Lesson**: when copying patterns between languages, watch for syntactic
ambiguity. And: when the UI silently breaks, ALWAYS check the browser
console first.

---

## Failed approaches

### LLM-based filename clustering

Hypothesis: an LLM could catch the residual cases that the deterministic
normalizer missed — different naming schemes, abbreviations, scrambled word
order.

Reality after testing:

- gemma4:26b returned empty responses 40% of the time. Reason unclear; one
  retry helped marginally.
- When it DID respond, the model was easily fooled by MegaPACK-style
  naming. Files named `Studio_Pack_00001.mp4` through `Studio_Pack_00500.mp4`
  represent 500 DIFFERENT scenes in one downloaded pack, not duplicates.
  The model with confidence 1.0 grouped 29 of 30 batch members together.
  Auto-deletion would have lost 28 unique scenes per false-positive group.
- Total yield over a full run: 3 small groups out of 781 LLM calls. The
  deterministic matcher had already caught the easy cases.

Mitigations added (in case the matcher is revisited):
- Tighter system prompt with explicit anti-MegaPACK negative examples
- Sanity cap: reject groups >50% of batch or >8 members
- JSON schema format for structured output (more reliable than format: "json")
- One retry on empty response
- Audit log of every call with prompt + response

Current status: **parked**. The deterministic matcher does the job. If
revisiting, consider trying qwen3:8b (often more reliable than gemma on
structured output), tightening the prompt with more examples specific to
this library's naming patterns, or abandoning the approach entirely.

### Bucketing by parent directory only

Initial filename matcher only bucketed by parent dir for phase 2. Missed
cross-directory dupes like `Studio/Show/file.mp4` vs
`whisparr/Studio/Show/file.mp4` because they had different parents.

Added grandparent bucketing too. Worked for that case but contributed to
the phase 2 hang on large libraries — now both `by_dir` and `by_grandparent`
could each independently contain a 10k-file bucket.

The eventual fix (sub-bucketing by content token) made both layers
tractable. But it's worth noting: more buckets = more chances for the
O(n²) loop to explode.

### O(n²) in the matcher

The fingerprint matcher (perceptual + chromaprint) still has unguarded
O(n²) behavior, even though it duration-buckets the candidates. With
~26k videos in production this hasn't been a problem yet, but the
filename-matcher experience suggests it will be eventually. Should be
refactored to chunked comparison before the library doubles.

---

## Things the operator does often

- **Apply changes from chat**: download files from the chat output, `cp`
  into place under `~/mediadedup/`, `docker compose build`, `docker compose
  up -d --force-recreate`, hard refresh browser.
- **Spot-check status**: `docker compose exec postgres psql -U mediadedup
  -c "SELECT media_type, status, COUNT(*) FROM files GROUP BY 1,2"`.
- **Run filename matcher**: dashboard button. Takes ~10 seconds. Look at
  the new groups before clicking auto-delete.
- **Run full matcher**: dashboard button. ONLY after the queue is fully drained
  (no `pending` or `processing` rows). Otherwise wastes work.
- **Look at recently-completed work**: dashboard "Recently completed" tile.
- **Investigate failures**: dashboard "Recent failures" tile shows the last
  failed jobs with their error messages.

---

## Things the operator avoids

- Restarting the stack when avoidable. Past experience says restarts trigger
  scanner runs that trigger the mass-missing bug. The new guards should
  prevent that, but the muscle memory of "don't restart" is still there.
- Running matcher and scanner simultaneously. Workers + matcher have OOM'd
  in the past.
- Trusting auto-delete on `match_type='llm'` without manual review. The
  false-positive rate is too high.
- Editing `.env` without backing it up first.
- `docker compose down -v`. The `-v` flag deletes Postgres data.

---

## Repository

Public at https://github.com/robertegardner/mediadedup. Pushed manually by
the operator after applying changes from chat. CI is not configured.

### Sanitization checklist before each commit

```bash
# No secrets
git grep -iE "password|secret|api[_-]?key|token" -- ':!*.example' ':!README*' ':!docs/*'

# No private IPs (the README has some examples in 192.168.* which are intentional,
# but anything new should be reviewed)
git grep -E "192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\." -- ':!*.example' ':!README*' ':!docs/*'
```

---

## Open issues and TODOs

These were identified during development but never fixed:

- **Matcher memory model**: load all fingerprints into RAM for pairwise
  comparison. OK at 26k videos / 145k audio, will break at 2-3x scale.
  Should be chunked.
- **Orphan run reaper**: the reaper handles stuck `files.status='processing'`
  rows but doesn't touch `orchestrator_runs.finished_at IS NULL`. If a
  matcher run crashes, the row stays orphaned forever. The
  reaper should sweep both tables.
- **Subkind in orchestrator_runs**: all matcher invocations record `kind='matcher'`,
  whether they're the filename matcher, perceptual matcher, or LLM matcher.
  Should add a `subkind` column for clearer audit.
- **Better progress reporting**: the matcher and LLM matcher have no
  meaningful progress indicator beyond "still running." For long runs this
  is frustrating. Should expose progress via the orchestrator_runs.stats
  JSONB column and read it in the UI.
- **Test coverage**: zero automated tests. The deterministic normalizer in
  particular would benefit from unit tests with edge cases pinned. The
  operator and an AI assistant have done all "testing" by running the system
  on real data, which has worked but is slow.

---

## Why this file exists

The operator and an AI assistant developed this project over a long series
of conversations, with extensive context that doesn't fit in any single chat
window. Most of the design decisions and bug lessons aren't visible from the
code itself — they're encoded in why-not-how form, in the comments and the
operator's memory.

This file is the codified version of "ask the operator about it." If you're
making nontrivial changes, read it first.
