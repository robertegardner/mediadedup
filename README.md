# mediadedup

A self-hosted Docker stack that finds duplicate (and *near-duplicate*) videos
and music across one or more network shares, GPU-accelerated via NVIDIA NVDEC.

## Features

- **Multi-source scanning.** Configure as many shares as you want (Whisparr,
  archives, family media, music libraries). One fingerprint database spans
  all of them, so duplicates across shares are found automatically.
- **Three detection algorithms**:
  - **Exact** — SHA-256 (partial hash on big files for speed) catches
    byte-identical copies.
  - **Perceptual (video)** — pHash over GPU-decoded keyframes catches
    re-encodes, resolution changes, container swaps.
  - **Chromaprint (audio)** — acoustic fingerprints catch the same recording
    across formats and bitrates.
- **Source-aware keeper selection.** Higher-priority sources beat lower
  ones; within priority, higher resolution × bitrate (video) or higher
  bitrate × size (audio) wins.
- **Non-destructive deletion.** Files are renamed to
  `<share>/.mediadedup-trash/YYYYMMDD/` — same filesystem, instant move,
  reversible until you manually `rm -rf` the trash.
- **Web UI** for review, bulk auto-delete (exact + similarity-thresholded),
  source management, manual + scheduled runs.
- **Built-in resilience**: stuck-row reaper, per-call I/O timeouts, periodic
  scheduler with overlap protection.

## Architecture

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   scanner    │───▶│    redis     │◀───│   worker(s)  │── GPU ──▶ ffmpeg
│ (orchestr.)  │    │  (RQ queue)  │    │              │           (NVDEC)
└──────┬───────┘    └──────────────┘    └──────┬───────┘
       │                                       │
       ▼                                       ▼
┌──────────────────────────────────────────────────────┐
│                      postgres                        │
│ sources / files / dup_groups / dup_members /         │
│ orchestrator_runs / action_log                       │
└──────────────────────────────────────────────────────┘
       ▲                                       ▲
       │                                       │
┌──────┴───────┐  ┌──────────────┐     ┌───────┴──────┐
│   matcher    │  │    doctor    │     │     web      │
│ (orchestr.)  │  │ (diagnostic) │     │   (FastAPI)  │
│              │  │              │     │ scheduler +  │
│              │  │              │     │ reaper inside│
└──────────────┘  └──────────────┘     └──────────────┘
```

All Python services run from a single Docker image. The web container hosts
the FastAPI UI, the periodic scheduler (APScheduler), and the stuck-row
reaper. Workers are scaled horizontally and consume RQ jobs. Scanner and
matcher are now wired into the web container too, so they can be triggered
from the dashboard and CLI; the standalone tool containers (`scanner`,
`matcher`) remain for one-shot ops.

---

## Prerequisites

A clean Ubuntu host (tested on 22.04 and 24.04) with:
- An NVIDIA GPU (consumer or workstation) and a working host driver. Verify
  with `nvidia-smi` before starting.
- Network access to the share(s) you want to scan.
- ~5 GB free disk for the Docker image and Postgres data.
- Docker and the NVIDIA Container Toolkit (the included `host-setup.sh`
  installs both).

---

## Installation

### 1. Extract and inspect

```bash
mkdir -p ~/mediadedup
cd ~/mediadedup
tar xzf /path/to/mediadedup.tar.gz --strip-components=1

# Verify all the files arrived. You should see 16 .py files and 4 .html files.
ls app/
ls app/templates/
```

Expected app files: `actions.py`, `bulk_actions.py`, `config.py`, `db.py`,
`doctor.py`, `ffmpeg_utils.py`, `matcher.py`, `orchestrator.py`, `phash.py`,
`reaper.py`, `scanner.py`, `scheduler.py`, `sources.py`, `web.py`,
`worker.py`, `__init__.py`.

Expected templates: `base.html`, `group.html`, `groups.html`, `index.html`.

If anything's missing, re-extract — don't proceed.

### 2. Install Docker + NVIDIA Container Toolkit

```bash
sudo ./host-setup.sh
```

Idempotent. Verifies `nvidia-smi` works, installs Docker Engine (not Snap),
installs NVIDIA Container Toolkit, wires it into Docker, runs a test
container to prove GPU passthrough. **Log out and back in** afterwards so
your shell picks up the new `docker` group membership.

### 3. Mount your shares on the host

mediadedup does *not* mount shares itself — it bind-mounts host paths into
the containers. You need host-side mounts for every share you want to
scan.

#### Option A: autofs (recommended for SMB/CIFS with occasional NAS reboots)

autofs mounts on access and unmounts when idle. No stale handles after the
NAS reboots.

```bash
sudo apt-get install -y autofs

# Master config: tell autofs that /mnt is managed
echo "/mnt /etc/auto.media --timeout=600" | sudo tee /etc/auto.master.d/media.autofs

# Per-share map. Add one line per share.
sudo tee /etc/auto.media <<'EOF'
whisparr -fstype=cifs,vers=3.1.1,sec=none,uid=1001,gid=100,file_mode=0777,dir_mode=0777,rsize=16777216,wsize=16777216,cache=strict,actimeo=60,hard ://192.168.6.177/media/plex/whisparr
music    -fstype=cifs,vers=3.1.1,sec=none,uid=1001,gid=100,file_mode=0777,dir_mode=0777,rsize=8388608,wsize=8388608,cache=strict,actimeo=60,hard    ://192.168.6.177/media/plex/music
archive  -fstype=cifs,vers=3.1.1,sec=none,uid=1001,gid=100,file_mode=0777,dir_mode=0777,rsize=16777216,wsize=16777216,cache=strict,actimeo=60,hard ://192.168.6.177/media/plex/archive
EOF

sudo systemctl restart autofs
ls /mnt/whisparr | head    # triggers a mount
ls /mnt/music | head
ls /mnt/archive | head
```

#### Option B: fstab

Static mounts at boot. Simpler but doesn't handle NAS reboots gracefully.

```
//192.168.6.177/media/plex/whisparr  /mnt/whisparr  cifs  rw,vers=3.1.1,sec=none,uid=1001,gid=100,file_mode=0777,dir_mode=0777,rsize=16777216,wsize=16777216,cache=strict,actimeo=60,hard,_netdev  0  0
//192.168.6.177/media/plex/music     /mnt/music     cifs  rw,vers=3.1.1,sec=none,uid=1001,gid=100,file_mode=0777,dir_mode=0777,rsize=8388608,wsize=8388608,cache=strict,actimeo=60,hard,_netdev   0  0
```

Then `sudo mkdir -p /mnt/whisparr /mnt/music && sudo mount -a`.

Either way, the result is one or more directories under `/mnt/` (or wherever
you choose), each containing a share. The dedup stack binds the parent
(`/mnt`) and treats each subdirectory as a *source*.

### 4. Configure `.env`

```bash
cp .env.example .env
$EDITOR .env
```

The only required settings are:

```bash
# Parent host directory that contains every share. Bound to /media in containers.
MEDIA_PATH=/mnt

# Database password — set this to anything
DB_PASS=change-me-please

# Web UI port
WEB_PORT=8088
```

The default `VIDEO_ROOT`/`MUSIC_ROOT` and `WORKER_REPLICAS` values are
sensible. See `.env.example` for the complete annotated list.

### 5. Build and start

```bash
docker compose build
docker compose up -d
```

The first time the web container starts, it auto-creates default sources
named `video` (`/media/video`) and `music` (`/media/music`) **if those paths
exist inside the container** (i.e., if you have host mounts at
`/mnt/video` and `/mnt/music`). Otherwise no sources are created — you'll
add them manually in the next step.

### 6. Configure sources via the dashboard

Open `http://<host>:8088`. At the top of the page is a **Sources** card.

For each share you want to scan, click "+ Add a source" and fill in:

| Field | Example | Meaning |
|---|---|---|
| Name | `whisparr` | Short label for the UI |
| Path | `/media/whisparr` | Path *inside the container*. Maps to `MEDIA_PATH/whisparr` on the host. |
| Media type | `video` / `audio` / `both` | What kinds of files to look for |
| Priority | `10` for "preferred", `0` for normal, `-5` for low | Higher = preferred keeper when dupes span sources |
| Notes | `Main TV/movies library` | Optional |

Delete any auto-bootstrapped sources whose paths don't exist (they show
"path missing" in red).

### 7. Verify GPU is wired up

```bash
docker compose --profile tools run --rm doctor
```

Mandatory checks before you start a long fingerprinting run. The probe
should report `PASS — NVDEC decode + hwdownload roundtrip succeeded`. If
not, fix it now — the workers will fall back to CPU and run 10x slower.

### 8. Start fingerprinting

From the dashboard's **Scanner & matcher** card, click **Run scanner**.
Or per-source, click the "Scan" button on a row in the Sources card.

Watch the **Live activity** panel — workers should start picking up files
within seconds. The throughput tile shows files/min and MB/sec.

For a large initial run, leave it overnight. The dashboard's ETA estimator
is conservative (5-minute rolling average), so the time will tighten as it
runs.

### 9. Cluster duplicates

Once the queue drains, click **Run matcher**. Takes seconds to a minute.
The "Duplicate groups" table populates with everything found.

### 10. Review and act

- Click a group to review members and pick a keeper manually
- Or use the **Auto-delete exact** card to one-click delete byte-identical
  dupes (safe, no ambiguity)
- Or use the **Auto-delete similarity matches above a threshold** card to
  bulk-delete near-duplicates at a confidence level you choose (default
  0.98; the slider previews counts live)

Files are MOVED to `<share>/.mediadedup-trash/YYYYMMDD/`, never unlinked.
Spot-check, then `rm -rf` the trash dir when satisfied.

---

## Day-to-day operations

### Adding a new share later

1. Add the mount on the host (autofs map or fstab + `mount -a`)
2. Click "+ Add a source" in the dashboard
3. Click "Scan" on the new source row (or wait for the next scheduled run)

No restart required.

### Scheduled runs

The default schedule is **daily at 04:00 UTC** (`DEDUP_SCHEDULE_CRON=0 4 * * *`).
The schedule, next run time, and any auto-delete flags are shown in the
"Schedule" panel of the Scanner & matcher card.

To enable automatic deletion on scheduled runs, set in `.env`:

```bash
DEDUP_AUTO_DELETE_EXACT=1                # 1 to auto-delete exact dupes
DEDUP_AUTO_DELETE_SIM_THRESHOLD=0.98     # auto-delete sim ≥ 0.98 (optional)
```

Then `docker compose up -d --force-recreate web` to pick up the change.

### Manual CLI access

The same operations are available via CLI for scripts/cron:

```bash
# Bulk auto-delete with no UI interaction
docker compose --profile tools run --rm bulk_actions preview
docker compose --profile tools run --rm bulk_actions auto --match exact --yes
docker compose --profile tools run --rm bulk_actions auto --threshold 0.98 --yes

# One-off scanner/matcher runs
docker compose --profile tools run --rm scanner
docker compose --profile tools run --rm matcher

# Diagnostic
docker compose --profile tools run --rm doctor
```

### Tuning detection thresholds

All knobs are in `.env`. After changing any of these, re-run **only** the
matcher — fingerprints don't have to be recomputed.

| Variable | Default | Notes |
|---|---|---|
| `VIDEO_PHASH_FRAMES` | 16 | Frames sampled per video for fingerprinting |
| `VIDEO_PHASH_THRESHOLD` | 12 | Max average Hamming distance (0-64) for a match. 12 is friendly to cross-resolution dupes (1080p ↔ 720p). |
| `VIDEO_DURATION_TOLERANCE` | 3 | ±N seconds when bucketing comparison candidates |
| `CHROMAPRINT_THRESHOLD` | 0.85 | Min similarity (0..1) for audio match |
| `WORKER_REPLICAS` | 4 | More for higher throughput; diminishing returns past ~8 |
| `SHA_TIMEOUT_SECS` | 600 | Per-file SHA timeout |
| `REAPER_STUCK_THRESHOLD_SECS` | 1800 | Reset stuck `processing` rows after N seconds |

---

## Troubleshooting

### Workers running but not processing

```bash
# Are they actually idle, or stuck?
docker compose exec postgres psql -U mediadedup -c "SELECT status, COUNT(*) FROM files GROUP BY 1;"

# If 'processing' has stuck rows, the reaper will fix them in <30 min,
# or force it manually:
docker compose exec postgres psql -U mediadedup -c \
  "UPDATE files SET status='pending', error=NULL, processing_started_at=NULL WHERE status='processing';"

# Re-queue everything pending
docker compose --profile tools run --rm scanner
```

### GPU not being used

```bash
docker compose logs worker | grep -i "gpu probe"
# Should say "NVDEC decode + hwdownload roundtrip succeeded".
# If it says FAILED, run doctor for details:
docker compose --profile tools run --rm doctor
```

### Share went stale

CIFS/SMB shares can return "Stale file handle" if the NAS reboots while
the dedup VM is up. Symptoms: `ls /mnt/share` errors out, workers fail
files with I/O errors.

Cleanest fix: autofs (set up in step 3, Option A). Quick fix:

```bash
docker compose stop worker
sudo umount -fl /mnt/<share>
sudo mount -a
docker compose up -d worker
```

If `modprobe -r cifs` is needed (when CIFS state is wedged kernel-side),
see the original setup notes — it's a niche recovery case.

### "Container has no GPU access"

```bash
docker run --rm --gpus all nvidia/cuda:12.3.2-base-ubuntu22.04 nvidia-smi
```

Should print your GPU. If not, the NVIDIA Container Toolkit isn't wired in:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Web UI returns 404 on /api/sources or /api/activity

The image is built from cached layers that pre-date the file. Rebuild with
no cache:

```bash
docker compose build --no-cache web
docker compose up -d --force-recreate web
```

### Backing up the fingerprint database

```bash
docker compose exec postgres pg_dump -U mediadedup mediadedup > dump.sql
```

To restore:

```bash
docker compose exec -T postgres psql -U mediadedup mediadedup < dump.sql
```

### Nuking everything to start fresh

```bash
docker compose down -v    # the -v deletes Postgres data
```

You'll have to re-scan from scratch.

---

## File structure

| File | Purpose |
|---|---|
| `docker-compose.yml` | Service definitions |
| `Dockerfile` | Single image used by all Python services |
| `host-setup.sh` | One-shot Docker + NVIDIA Container Toolkit installer |
| `requirements.txt` | Python dependencies |
| `init.sql` | Postgres schema for fresh installs |
| `.env.example` | Annotated template for `.env` |
| `app/config.py` | Env-var-driven configuration object |
| `app/db.py` | Database connection + idempotent migrations |
| `app/sources.py` | Source CRUD and path-to-source resolution |
| `app/scanner.py` | Walk sources, register files, enqueue jobs |
| `app/worker.py` | RQ job: fingerprint one file |
| `app/ffmpeg_utils.py` | GPU/CPU frame extraction wrappers |
| `app/phash.py` | SHA + perceptual + Chromaprint helpers |
| `app/matcher.py` | Cluster fingerprinted files into dup groups |
| `app/actions.py` | Shared deletion / auto-mark logic |
| `app/bulk_actions.py` | CLI for bulk operations |
| `app/orchestrator.py` | Run-tracking for scanner/matcher with mutual exclusion |
| `app/scheduler.py` | APScheduler glue for periodic runs |
| `app/reaper.py` | Background thread that resets stuck rows |
| `app/web.py` | FastAPI app + dashboard endpoints |
| `app/doctor.py` | Diagnostic CLI |
| `app/templates/*.html` | Jinja2 templates for the web UI |

---

## What's *not* included

- **Network-share auto-detection.** You configure host mounts yourself.
- **Cross-host deduplication.** Single-host, single fingerprint DB.
- **Automatic restoration from trash.** Move them back manually if needed.
- **Live preview/playback in the web UI.** Thumbnails only; click out to
  play in your usual app.
- **Account/auth.** Don't expose this to the open internet.
