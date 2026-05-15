# CLAUDE.md

Project-memory file for Claude Code. Read this in full at the start of any
session before making changes. Conventions and gotchas here override any
generic best-practice instincts.

## What this project is

A self-hosted Docker stack that finds duplicates in a video and music library
mounted from a NAS. Single Postgres database, multiple workers, GPU-accelerated
fingerprinting. Operator interacts via a FastAPI web UI on port 8088.

The library is large (~26k videos, ~145k audio files) and lives on an unRAID
NAS at 192.168.6.177, mounted to the dedup VM via autofs CIFS at /mnt/whisparr
and /mnt/music. The containers see those host mounts at /media/whisparr and
/media/music.

This is a home-lab system, not production software. Reliability matters but
five-nines uptime does not.

## Architecture

Single Docker image (`mediadedup:latest`) runs every Python service:

- **web** (long-running): FastAPI app on :8088. Hosts the dashboard, the
  APScheduler-driven periodic runs, the stuck-row reaper thread, and the
  in-process orchestrator that handles scanner/matcher invocations.
- **worker** (×6): RQ consumers. Pull jobs from Redis, fingerprint one file
  each, write back to Postgres.
- **postgres** (16-alpine): the single source of truth.
- **redis** (7-alpine): RQ job queue only; no app data lives here.
- **scanner / matcher / doctor / bulk_actions** (tools profile): one-shot CLI
  tools, run via `docker compose --profile tools run --rm <name>`. The same
  logic also runs inside the web container.

Data flow: scanner walks sources → enqueues file IDs in Redis → workers pull,
fingerprint, write back → matcher clusters by exact/perceptual/chromaprint/
filename/LLM → operator reviews or auto-deletes via web UI → files MOVED
(never unlinked) to `<source>/.mediadedup-trash/YYYYMMDD/`.

## Sources

Multi-source design. A "source" is a row in the `sources` table mapping a
name to a container-side path with a priority. Files have a `source_id`
foreign key. Keeper selection prefers higher source priority.

Current sources in production:

| id | name     | path             | media_type | priority |
| -- | -------- | ---------------- | ---------- | -------- |
| 3  | music    | /media/music     | audio      | 0        |
| 35 | Whisparr | /media/whisparr  | video      | 0        |

Adding a source: dashboard's "+ Add a source" button. The path must already
exist inside the container (i.e., the host bind mount must be working).

After changing source paths, run the API `POST /api/sources/backfill` with
`only_null=false` to re-resolve every file row's `source_id` based on
longest-prefix path matching.

## Matchers

Five match algorithms, all populate `dup_groups` with a distinct `match_type`:

1. **exact**: SHA-256 collision. Always trustworthy.
2. **perceptual** (video): 16 keyframes per video, pHash hamming distance.
   Threshold 12 (out of 64). Catches cross-resolution and re-encode dupes.
3. **chromaprint** (audio): acoustic fingerprint similarity. Threshold 0.85.
   Catches the same song across formats.
4. **filename**: deterministic normalize + token-set jaccard. Free, fast,
   catches the lion's share of dupes in libraries fed by automated
   downloaders. Operates on `done`+`pending`+`processing` (no fingerprint
   needed).
5. **llm**: optional pass via Ollama on a separate GPU host. Operates on
   files NOT in any group yet. Currently disappointing — see "LLM matcher
   notes" below.

The CHECK constraint on `dup_groups.match_type` enumerates all five values.
If adding a sixth, add it to BOTH `init.sql` (for fresh installs) AND the
ALTER in `app/db.py` `_TABLE_MIGRATIONS` (for existing installs).

## Important conventions

### File creation and editing

- The operator strongly prefers receiving COMPLETE rewritten files over
  partial diffs or instructions. When making any nontrivial change, output
  the full file content.
- All output files for the operator go to `/mnt/user-data/outputs/` and are
  presented via the `present_files` tool. Don't dump code into the chat
  inline if you have the option to ship it as a file.

### Database migrations

- Schema for fresh installs lives in `init.sql`.
- Idempotent migrations for upgrades live in `_TABLE_MIGRATIONS` in
  `app/db.py`. These run in a pg_advisory_lock under a stable key
  (0x4D454449_44455550, ascii "MEDIDEUP" truncated) so only one process
  migrates at a time.
- After adding a migration, also update `init.sql` so fresh installs and
  upgrade installs end up in the same state.

### Background work in the web container

The web container hosts not just FastAPI but also:
- APScheduler (periodic scanner+matcher run at 04:00 UTC daily)
- The stuck-row reaper thread
- Manual scanner/matcher invocations triggered via UI buttons

When triggered from a button, the work runs in a daemon thread spawned from
the FastAPI endpoint, NOT in the request handler. The endpoint returns
immediately with `{"ok": true}` and the work proceeds in the background.

Tracking happens via `orchestrator_runs` (started_at, finished_at, error,
stats JSONB) with mutual exclusion enforced by an in-process threading.Lock.

**If a background thread crashes, it must update its `orchestrator_runs`
row before exiting.** A row with `finished_at=NULL` and no live thread is
an "orphan run" that confuses the UI. The `_run_one` wrapper in
`orchestrator.py` handles this via try/except, but always sanity-check new
background work.

### Deletion semantics

Files are MOVED to `<source>/.mediadedup-trash/YYYYMMDD/`, never unlinked.
- Same filesystem, so rename is atomic and instant
- Reversible: the operator can move files back manually
- Trash accumulates indefinitely until manually `rm -rf`'d

Never add code that calls `os.unlink` or `shutil.rmtree` on user content.

### Auto-delete confidence tiers

- **exact**: safe to auto-delete; one-click
- **perceptual** + **chromaprint** above similarity threshold: safe with
  threshold slider (default 0.98)
- **filename** resolution_only: safe; the normalized title is identical,
  only the resolution tag differs
- **filename** name_differs: review first; the titles share most tokens
  but aren't identical
- **llm**: review first; high false-positive risk (see below)

### Don't run matcher during heavy worker activity

The fingerprint matcher (perceptual + chromaprint) loads all fingerprints
into memory for pairwise comparison. With workers also using memory and
producing DB writes, running both at once risks OOM and slows both jobs.
Wait for the queue to drain before clicking "Run matcher".

The filename matcher is fine to run anytime — it's cheap.

## Known gotchas

### Mass-missing on mount glitches (FIXED)

Symptom: after restart, every file flips from `done` to `missing`. Status
counts show 0 in `done`.

Cause: scanner's `mark_missing_for_source` ran before autofs/CIFS had
re-mounted the SMB shares. `os.path.exists` returned False for every path,
so every row got marked missing.

Fix is in `app/scanner.py`'s `mark_missing_for_source`:
1. 60-second startup grace (no missing-detection in first minute of process life)
2. Path-alive check (refuse if source's top-level dir lists no entries)
3. 10% percentage cap (refuse if it would flip >10% of source's files)

Recovery if it happens again:

```sql
UPDATE files
   SET status='done', error=COALESCE(error,'')||E'\nrecovered from spurious missing'
 WHERE status='missing' AND sha256 IS NOT NULL;
UPDATE files
   SET status='pending', error=NULL, attempts=0
 WHERE status='missing' AND sha256 IS NULL;
```

### Music deletions fail with Permission denied (FIXED)

Symptom: `shutil.move` on `/media/music/...` raises `[Errno 13] Permission
denied` even though the host can write there fine. Whisparr deletions work
because whisparr happened to be mounted when the container started; music was
idle and never got its mount.

Root cause: Docker bind-mounts `/mnt` into containers with default `rprivate`
propagation. When autofs triggers a CIFS mount on the host **after** the
container started, the new mount lives only in the host's mount namespace and
never propagates in. The container sees the autofs ghost directory and gets
EPERM when it tries to traverse it.

Fix (deployed 2026-05-15):
1. All services in `docker-compose.yml` that mount `/mnt` now use long-form
   bind syntax with `propagation: slave`. This lets host-side autofs mounts
   propagate into containers, and container accesses to an unmounted autofs
   path retrigger the mount on the host (which then propagates back in).
2. `/etc/auto.master` timeout raised from 60 s to 600 s (was supposed to be
   600 all along — it had drifted).

After changing `docker-compose.yml`, restart affected containers:
```bash
docker compose up -d web worker
```
Autofs change requires:
```bash
sudo sed -i 's/--timeout=60/--timeout=600/' /etc/auto.master
sudo systemctl restart autofs
```

### SMB stale file handles

Symptom: workers fail with "Stale file handle" or I/O errors after NAS reboot.

Fix (already deployed): autofs with `--timeout=600` so mounts re-establish
on access. Manual recovery:
```bash
docker compose stop worker
sudo umount -fl /mnt/<share>
sudo mount -a
docker compose up -d worker
```

### Filename matcher hang on large buckets (FIXED)

Symptom: filename matcher starts, prints "Phase 1: N groups", never returns.

Cause: phase 2 did all-pairs comparison within each parent-directory bucket.
On a library with many files in one directory (e.g. /media/whisparr/ root),
single buckets could exceed 10,000 files → 50M+ pair comparisons → hours.

Fix in `app/filename_match.py`: buckets > MAX_BUCKET_SIZE (200) get
sub-divided by first alphabetic content token. Sub-buckets > MAX_SUB_BUCKET_SIZE
(400) get skipped with a warning. Global PAIR_COMPARE_BUDGET (2M) is the
last-resort circuit breaker.

### Database disk space

Postgres lives on `/var/lib/docker` on the dedup VM's root LV. The LV is
sized at 32GB (after a previous extension). Postgres data is ~1.1 GB but
WAL can spike during big operations (matcher run, bulk migrations).

If `df -h /` shows < 20% free, prune Docker images:
```bash
docker image prune -a -f
docker builder prune -a -f
```

Also extend the LV if needed:
```bash
sudo vgs              # check VG free space
sudo lvextend -L +10G /dev/mapper/ubuntu--vg-ubuntu--lv
sudo resize2fs /dev/mapper/ubuntu--vg-ubuntu--lv
```

### Docker log rotation

Default `json-file` driver doesn't rotate. Configured globally in
`/etc/docker/daemon.json`:
```json
{
  "log-driver": "json-file",
  "log-opts": {"max-size": "50m", "max-file": "3"}
}
```
If this isn't there, set it and `sudo systemctl restart docker`.

## LLM matcher notes

Wired up but currently disappointing. Three issues observed in production:

1. **Gemma 4 returns empty responses ~40% of the time.** Cause unknown;
   suspect context-shift hiccups. We added one retry on empty response but
   it doesn't help much.
2. **False positives on "MegaPACK" content libraries.** Filenames like
   `Studio_Pack_00001.mp4` through `Studio_Pack_00500.mp4` are sequentially
   numbered DIFFERENT scenes in one pack. The LLM (gemma4:26b at confidence
   1.0) grouped 29 of 30 as "duplicates with varying UUIDs". Sanity caps
   (>50% of batch OR >8 members → reject) catch this now, but the underlying
   prompt issue is unresolved.
3. **Low yield.** A full run produced 3 small groups out of 781 LLM calls.
   The deterministic filename matcher already caught the easy cases.

The LLM matcher is parked. If revisited, consider:
- Trying qwen3:8b instead (more reliable JSON output than gemma4)
- Tightening the prompt further with more negative examples
- Bucketing by something other than parent_dir (most singletons live
  alongside unrelated files, so the LLM rarely sees real candidates together)
- Possibly: skip the LLM matcher entirely for this library structure

Settings are in the `settings` table, keys prefixed `llm.`. Configurable
via web UI panel inside the LLM card.

## Ollama hosts

- 192.168.85.61:11434 — 5090, larger models (gemma4:26b etc.)
- 192.168.6.164:11434 — 3080, smaller models

Both require `OLLAMA_HOST=0.0.0.0:11434` in the systemd unit to be reachable
from the dedup VM. If reachability is broken, test from inside the web
container first (`docker compose exec web python -c "import httpx;
print(httpx.get('http://HOST:11434/api/tags').json())"`).

## File ownership and permissions

Container runs as root (uid 0). SMB mounts use `uid=1001,gid=100,
file_mode=0777,dir_mode=0777`. The 1001/100 maps to the host user; root
inside the container can write anywhere on the share because of 0777.

Don't change container uid without also adjusting SMB mount options.

## Environment configuration

`.env` is git-ignored; `.env.example` is committed. Required keys:
- `MEDIA_PATH=/mnt` (host parent dir, bound to /media in containers)
- `DB_PASS=...` (Postgres password)
- `WEB_PORT=8088` (host port for the dashboard)

Default `WORKER_REPLICAS=6` works for the current VM size (24 GB RAM, 4 cores).
Going higher risks OOM during matcher runs.

## When something breaks

1. **Check `docker compose ps`** first. Anything not running or exited?
   Exit code 137 = SIGKILL, usually OOM. Exit code 0 with warm shutdown
   = something explicitly stopped it.
2. **`docker compose logs --tail=200 <service>`** for the relevant service.
3. **Postgres state**: `docker compose exec postgres psql -U mediadedup` for
   ad-hoc queries. Status distribution: `SELECT media_type, status, COUNT(*)
   FROM files GROUP BY 1,2`.
4. **Orphan runs**: `SELECT * FROM orchestrator_runs WHERE finished_at IS NULL`.
   Clear with `UPDATE orchestrator_runs SET finished_at=NOW(), succeeded=FALSE,
   error='manually cleared' WHERE finished_at IS NULL`.

## Repo on GitHub

https://github.com/robertegardner/mediadedup — public. The operator pushes
manually after applying changes. Before commit, sanity check:

```bash
git grep -iE "password|secret|api[_-]?key|token" -- ':!*.example' ':!README*' ':!CLAUDE.md'
```

Should be empty.

## Things the operator cares about

- **Brevity in chat.** Long pre-ambles are unwelcome. Lead with the answer.
- **Honest assessments.** If a feature isn't working well, say so. Don't oversell.
- **Don't make destructive moves without confirming.** Especially anything
  that touches `dup_groups`, `files`, or `.env` — confirm first.
- **Mention thumbs-down if a behavior seems wrong.** The operator may want
  to provide feedback to Anthropic.
