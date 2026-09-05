# Hybrid planar-base slicing on Render: investigation, test, and fix

Dated 2026-09-05. Written as a record of what was found and why the fix was
built this way, not as ongoing documentation of current behavior -- if the
code this describes changes later, trust the code and git history over this
file, the same way CLAUDE.md asks for any print-tested or deploy-tested
claim.

## 1. The report

The hybrid planar-base feature (a real OrcaSlicer-sliced solid base plus this
app's own non-planar spiral wall on top, see the `hybrid-orca-planar-base`
project memory for how that feature was originally built) worked from a
local browser hitting `python serve.py` directly, but failed for the same
user on the deployed site, `https://trident.mmucybertron.com/`.

## 2. Root cause

`serve.py` resolves the OrcaSlicer CLI binary via `orca_binary_path()`
(`trident_gcode/orca.py`), which checks (in order): an explicit path, the
`TRIDENT_ORCA_PATH` env var, `shutil.which("orca-slicer")`, then a short list
of common install locations. Locally, this found the user's actual OrcaSlicer
install (`C:\Program Files\OrcaSlicer\orca-slicer.exe`). On Render, none of
that exists -- the deployment was `runtime: python` (Render's native Python
buildpack), which has no way to install an arbitrary system binary.

Confirmed live on the deployed site (not assumed) by fetching
`/api/orca_status`:

```json
{"available": false, "path": null}
```

`serve.py` already anticipated exactly this and fails cleanly rather than
silently:

```python
raise ValueError(
    "Hybrid planar base requires a local OrcaSlicer install. ..."
    "Not available on a hosted deployment."
)
```

...and the frontend (`viewer/designer.js`'s hybrid/mesh-hybrid capability
probes) already disables the relevant controls up front when
`/api/orca_status` reports `available: false`, rather than letting a user
configure a hybrid print and only discover it's broken after clicking
Generate. So the *reported* failure was not a bug in the strict sense -- it
was working exactly as designed, for a limitation the design fully expected.
The open question was whether that limitation could be lifted.

## 3. Two different questions, and which one mattered

"OrcaSlicer isn't installed on Render" resolves two different ways:

1. Make the *disabled* state clearer in the UI (quick, no risk, keeps the
   feature local-only).
2. Actually get a real OrcaSlicer binary running inside Render's container so
   hybrid mode works for hosted users too.

(2) was chosen. Before touching any deployment config, two prior questions
had to be answered first, because getting them wrong would mean shipping a
guess to a public site:

- Does switching Render's runtime to Docker (required to install a custom
  binary) force a paid plan?
- Even if free-tier Docker is allowed, does OrcaSlicer's CLI actually run
  inside Render's free-tier resource envelope at all?

## 4. Render's actual free-tier facts (checked via web search, 2026-09-05)

- Free web services: **512 MB RAM / 0.1 vCPU**.
- Spin down after **15 minutes** of inactivity; next request pays a
  **30-60s cold start**.
- **750 free instance-hours/month**; free tier bandwidth was cut from 100 GB
  to **5 GB/month** in an April 23, 2026 pricing revamp.
- Docker-based web services are supported on the free tier the same as
  native-runtime services -- switching `runtime: python` to `runtime: docker`
  is **not itself a billing event**.
- Lowest paid *compute* tier (if free ever proves insufficient): about
  **$7/month** for 0.5 vCPU / 512 MB, separate from the $25/month "Pro"
  *workspace* plan (a team-plan fee, not required for a single paid service).
- **Not found anywhere in Render's own docs**: a documented HTTP/proxy
  request timeout. Comparable platforms (Cloudflare, DigitalOcean App
  Platform) commonly use ~100s, but that number does not transfer to Render
  on any evidence found -- this remains a genuine unknown, not a verified
  fact, and is the one open risk this whole effort could not close from a
  desk before deploying (see Section 8).

## 5. Feasibility probe (built and run before touching production config)

`tools/orca_render_feasibility/` (kept in the repo, untracked from
production -- see its own `.dockerignore` entry) is a throwaway harness built
specifically to answer "does this even run under Render's exact caps"
*before* committing to a real deployment change:

- `Dockerfile`: Ubuntu 24.04 (matching the OrcaSlicer 2.4.2 Linux AppImage
  release asset's own target -- `OrcaSlicer_Linux_AppImage_Ubuntu2404_
  V2.4.2.AppImage`, confirmed via GitHub's release API, not guessed),
  installs a GTK3/webkit2gtk dependency set, extracts the AppImage (no FUSE
  in most PaaS containers), and provides two candidate invocations: the
  AppImage's `AppRun` directly, and an `xvfb`-wrapped fallback in case Orca's
  `--slice` path needed a display server.
- `run_feasibility_test.py`: runs the REAL `build_hybrid_print()` /
  `build_mesh_hybrid_print()` code path (not a stub -- unlike
  `tools/check_regression.py`, which deliberately stubs the Orca subprocess
  for byte-identical determinism), measuring wall time and child RSS.
- `run_feasibility.ps1`: builds the image and runs it with
  `docker run --memory=512m --cpus=0.1 --memory-swap=512m` -- Render's exact
  free-tier caps, no swap.

Docker Desktop was not installed on the test machine at the start of this
work; it had to be installed (`wsl --install`, then
`winget install -e --id Docker.DockerDesktop`) before any of this could be
verified for real rather than assumed.

### First pass: does the binary even start?

Result: **yes, on the first try, direct (no xvfb needed)**. A small circle
hybrid print (10mm radius, 20mm height) completed in 9.4s wall time under the
exact caps, peak child RSS 100.9 MB. This ruled out the biggest open
question (a GUI-toolkit app refusing to run headless) immediately.

### Second pass: real designs, not a toy case

Per request, two heavier, more realistic scenarios were added and run:

| Scenario | What it exercises | Wall time | Result |
|---|---|---|---|
| `small_circle` | baseline sanity check (10mm radius circle, 20mm height) | 9.4s | PASS |
| `star_zones` | star shape + **3 overlapping Zone Overrides** (diamond/hammered/pleats patterns, one with `xy_twist_turns`), production `points_per_turn=240`, 50mm wall height -> 37680 total wall points | **65.1s** | PASS |
| `mesh_stl` | a real user file, `Lamp Mount Test.stl` (2728 triangles, 64x64x5mm, loaded via `load_stl` and inspected before the test -- not guessed), through `build_mesh_hybrid_print` | 26.2s | PASS |

All three passed -- no OOM, no crash, correct G-code output (verified via
`analyze_gcode`: zero issues, zero probe hits; the mesh case's own
`footprint_radius_mm: 32.01` independently confirmed the chosen test
parameters matched the real mesh's actual footprint rather than being
arbitrary).

**Measurement caveat, disclosed rather than hidden**: `peak_child_rss` was
read via `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`, which is a
*monotonic watermark across the whole process*, not a per-call reset. Since
all three scenarios ran in one container invocation, the individual
100.9/15.9/47.6 MB figures can misattribute which scenario actually used
what memory. The trustworthy fact is the **combined final watermark, ~164
MB** -- a hard ceiling on any single scenario's true peak (RSS only
climbs), comfortably under the 512 MB cap either way. Wall-clock times are
unaffected by this (independent stopwatch per scenario) and are solid.

### The real finding

`star_zones` at 65.1s (vs. 14.0s unconstrained locally, a ~4.6x slowdown) is
dominated by **pure-Python wall geometry math** (`zone_weight`/
`zone_twist_integral` per point in `trident_gcode/paths.py`), not the Orca
subprocess -- and `points_per_turn` is hardcoded to 240 for both hybrid
endpoints server-side (`serve.py`, not user-configurable), so this scales
directly and only with **wall height in layers**. A taller or more heavily
zoned design would take proportionally longer, entirely independent of
whether Orca itself would have fit. This is the finding that justified a
server-side complexity gate rather than shipping with no ceiling at all.

## 6. What got built (production, not the throwaway probe)

- **`Dockerfile`** (repo root): the real deployment image. Same proven
  recipe as the feasibility probe -- OrcaSlicer 2.4.2 AppImage, extracted,
  `TRIDENT_ORCA_PATH=/opt/orcaslicer/AppRun` (the "direct, no xvfb" path that
  actually passed every scenario). The xvfb wrapper script is still installed
  as a same-image rescue path (flip one env var, no rebuild) in case a future
  Orca version regresses that finding.
- **`render.yaml`**: `runtime: python` -> `runtime: docker`,
  `dockerfilePath: ./Dockerfile`, `buildCommand`/`startCommand` removed (the
  Dockerfile's own `CMD` replaces `startCommand` for a Docker service). Adds
  two new env vars (see below). `PYTHON_VERSION` (the old native-runtime pin)
  has no Docker equivalent -- the `FROM ubuntu:24.04` line now fixes the
  interpreter (ships Python 3.12, still satisfies the `X | None` 3.10+
  requirement).
- **`serve.py`**: new `_hybrid_complexity_limits()` gate, checked once,
  before any Orca subprocess or STL/geometry work starts (fail-fast, same
  discipline as the existing bed-size/z_max pre-flight checks right next to
  it). Reads two env vars:
  - `TRIDENT_MAX_HYBRID_WALL_POINTS` -- estimated `(upper_wall_layers + 1) *
    240` against this ceiling, for BOTH hybrid endpoints (parametric
    silhouette and mesh base).
  - `TRIDENT_MAX_HYBRID_MESH_TRIANGLES` -- the mesh-hybrid endpoint's own
    input triangle count against this ceiling.

  **Both are `None` (unlimited) when unset** -- this is a deliberate
  departure from CLAUDE.md's usual "missing config becomes a conservative
  default" rule, because that rule is about a *machine safety* limit
  silently inheriting a different printer's number (e.g. `max_z_velocity`).
  This gate guards a *hosting resource budget* instead, which simply does
  not exist for a local/self-hosted run with no request timeout -- an
  artificial cap there would be a made-up restriction on a machine it was
  never measured against. Only Render's own `render.yaml` opts a specific
  deployment into a specific number, the same pattern `TRIDENT_BIND` already
  uses (safe/restrictive by default, a specific deployment explicitly widens
  or, here, narrows it for itself).

  Also fixed: the "Not available on a hosted deployment" error text was now
  stale (it *can* be available) -- reworded to describe a real deployment
  problem instead of a blanket unavailability claim.
- **`viewer/index.html`**: the two static hint strings ("...unavailable on a
  hosted deployment") had the same staleness -- both already only *display*
  when `/api/orca_status` reports unavailable (no JS logic changed, that part
  was already correct), just reworded so the text itself doesn't overclaim.
- **`.dockerignore`** (repo root): keeps `.git`, `scratchpad`,
  `tools/orca_render_feasibility` (the dev-only probe) out of the production
  image.

### The chosen numbers, and why they are first-pass, not derived

```yaml
TRIDENT_MAX_HYBRID_WALL_POINTS: "60000"     # ~1.6x the one tested value (37680)
TRIDENT_MAX_HYBRID_MESH_TRIANGLES: "8000"   # ~3x the one tested value (2728)
```

These give headroom above the one real measurement each, not a validated
worst case -- there is only one real data point per metric. In points-of-
wall-height terms, `TRIDENT_MAX_HYBRID_WALL_POINTS=60000` allows roughly:

| Layer height | Max non-planar wall height before rejection |
|---|---|
| 0.30 mm | ~74.7 mm |
| 0.20 mm | ~49.8 mm |
| 0.15 mm | ~37.4 mm |

A tighter alternative (40000, closer to the one tested value) was considered
and deliberately NOT taken -- decided to deploy with 60000 and treat the live
result as the real test, on the reasoning that typical real hybrid usage
(e.g. the `Lamp Mount Test.stl` case: a short mechanical base, modest wall on
top) sits nowhere near this ceiling, and the gate still exists as a backstop
against genuinely extreme requests either way.

## 7. Validation actually performed (not assumed)

- `tools/check_regression.py` and `tools/test_printer_import.py`: **ALL PASS**,
  byte-identical, both before and after every code change in this effort --
  the new gate is provably inert unless its env vars are explicitly set.
- The production `Dockerfile` was built and run locally (unconstrained, then
  again under the exact Render caps) -- not just written and assumed correct:
  - `/api/orca_status` on the running container: `{"available": true, "path":
    "/opt/orcaslicer/AppRun"}`.
  - A real hybrid `/api/generate` HTTP request against the running container
    produced valid G-code (`M83` relative extrusion confirmed present --
    the CLAUDE.md invariant this whole project is built around).
  - With the gate deliberately set to a low test cap (5000), an oversized
    request was rejected in well under a second with a clear message citing
    the actual estimated point count; a request under that same cap still
    succeeded normally in the same run.

## 8. What remained unverified before deploying (and how each was resolved)

Documented explicitly rather than glossed over, because CLAUDE.md's own
ethos is that unverified claims get labeled as such. All four were open
questions at commit time; Section 11 records how each was actually settled.

- **Render's actual HTTP/proxy request timeout was not documented anywhere
  found.** The `star_zones` case (65.1s) plus a cold start (30-60s after 15
  min idle) could plausibly stack past the ~100s other platforms in this
  class commonly use. RESOLVED (Section 11): the real production number for
  that same case was 30.0s, well clear of that ballpark.
- All container-level testing ran on local Docker Desktop against Render's
  *documented* specs, not Render's actual production infrastructure.
  RESOLVED: real production numbers were faster than the local simulation,
  not slower -- see Section 11.
- Every scenario ran alone, once -- no concurrent-request behavior was
  tested, and free tier is a single instance. STILL UNVERIFIED -- nothing
  in Section 11 tested concurrency.
- The per-scenario memory attribution caveat from Section 5 (bounded above
  by ~164 MB combined, but individual scenario numbers are approximate).
  Unaffected by Section 11 -- still the best available bound.

## 9. Decision made before deploying

Decided: deploy as-is (`TRIDENT_MAX_HYBRID_WALL_POINTS=60000`, not tightened
further), and treat the first real live requests as the actual missing test
for Section 8's open questions, on the reasoning that typical real hybrid
usage (e.g. the `Lamp Mount Test.stl` case: a short mechanical base, modest
wall on top) sits nowhere near the tested ceiling anyway.

## 10. Pre-deploy file map

| File | Role |
|---|---|
| `Dockerfile` | production deployment image |
| `.dockerignore` | keeps dev-only files out of the production image |
| `render.yaml` | Render blueprint: docker runtime + the two gate env vars |
| `serve.py` | `_hybrid_complexity_limits()` gate + reworded error text |
| `viewer/index.html` | reworded static hint text (logic unchanged) |
| `tools/orca_render_feasibility/` | throwaway feasibility probe, kept for whenever Orca or Render's caps change and this needs re-checking |

## 11. The actual deployment -- what happened, not what was planned

Pushing the `render.yaml`/`Dockerfile` commit to `main` did **not** convert
the existing Render service to Docker. Confirmed on the dashboard: the
service still showed the `Python 3` badge and its Build Command was still
`python --version` (the old native-runtime command), and the deploy that
did run only took 50.4s -- far too fast to have pulled Ubuntu, installed
~150 MB of apt packages, and downloaded the OrcaSlicer AppImage.

**Root cause**: Render does not let an already-provisioned service's
runtime type (native vs. Docker) change via `render.yaml` alone. Checked
every Settings tab (General/Build/Deploy/Custom Domains/Networking/Edge
Caching/Notifications/Health Checks/Maintenance Mode/Delete or suspend) --
there is no "change runtime" control anywhere. This is a genuine Render
platform limitation, not a mistake in the blueprint.

### 11.1 The fix: a second, new service

The only path is a **new** Web Service created with Docker selected from
the start, pointed at the same GitHub repo. Created via Render's normal
"New -> Web Service" flow (not "New -> Blueprint", to avoid any ambiguity
about how Render would reconcile a `render.yaml` whose service `name:`
already matches an existing service):

- Repo: `ryan0304/Non-Planar-Slicer`, branch `main` -- Render auto-detected
  the Dockerfile ("It looks like you're using Docker, so we've autofilled
  some fields accordingly") and correctly pre-filled Language=Docker,
  Region=Singapore.
- **Instance type defaulted to the $7/month paid plan**, not free -- this
  is NOT pre-selected as free and is easy to miss. Had to be explicitly
  changed to the $0/month option.
- The three env vars from `render.yaml` (`TRIDENT_BIND`,
  `TRIDENT_MAX_HYBRID_WALL_POINTS`, `TRIDENT_MAX_HYBRID_MESH_TRIANGLES`)
  had to be added by hand -- a plain "New Web Service" does not read
  `render.yaml`'s `envVars` section.
- Named `Non-Planar-Slicer-Docker` (service ID `srv-dadsmiou01pc73c5esi0`),
  since the name `Non-Planar-Slicer` was already taken by the old service.

Real Docker build this time (visibly "Building" for 59s+ in the dashboard,
not the suspicious 50.4s from before). Once live, its own
`https://non-planar-slicer-docker.onrender.com/api/orca_status` returned
`{"available": true, "path": "/opt/orcaslicer/AppRun"}` -- confirmed on the
first try, matching every finding from the local feasibility probe.

### 11.2 Real production numbers (not simulated) -- Section 8's timeout question, answered

Sent directly to the new service's own onrender.com URL, before touching
the live domain, so the old service stayed untouched and serving in case
anything went wrong:

| Request | Result | Time |
|---|---|---|
| Light (10mm circle hybrid) | 200 OK | 12.6s |
| Heavy (star + 3 zone overrides, same shape as the feasibility probe) | 200 OK | **30.0s** |

30.0s in real production is *faster* than the 65.1s the local Docker
Desktop simulation predicted for the identical case -- Render's real
infrastructure outperformed the local stand-in, for whatever reason
(better single-core turbo, less virtualization overhead, plain variance).
This resolved Section 8's biggest open question well within a comfortable
margin of the ~100s ballpark other platforms use, without ever learning
Render's actual documented number (still not found).

**A separate, unrelated bug found along the way**: the first heavy-request
attempt used serve.py's *default* star shape (`star_points=5,
star_depth=0.35`, since the test request omitted those fields) and got a
400 from `hybrid.py`'s placement-sanity check: *"OrcaSlicer placed the base
off-target (bounding-box center 119.8,117.5 vs intended 117.5,117.5)"*. The
6-point star used throughout the feasibility probe never hit this. Not
investigated further (out of scope for this deployment effort) -- plausibly
related to a 5-point star's bounding box not sharing the shape's own
rotational symmetry, but that is a guess, not a diagnosis. Worth a look
separately; not a resource or Render-specific issue.

### 11.3 The domain cutover: harder than expected, briefly caused a real outage

Attempting to add `trident.mmucybertron.com` to the new service while it
was still attached to the old one failed outright: *"This domain is
already in use on Non-Planar-Slicer. Please delete it from that service and
try again."* Render enforces one service per domain, with no atomic
handoff.

Consequence: removing the domain from the old service to free it up
**immediately took the live site down** -- confirmed directly
(`curl https://trident.mmucybertron.com/api/orca_status` started returning
Cloudflare's own error page rather than JSON).

That surfaced a fact not previously known going into this: **the domain is
proxied through Cloudflare**, not pointed directly at Render. The error was
Cloudflare's own **Error 1000, "DNS points to prohibited IP"** -- a
Cloudflare-side rejection, not a Render error.

Fix turned out simpler than feared: adding the domain to the new service in
Render **verified immediately** (green checkmark, before any DNS change),
and once Render finished issuing a fresh certificate for it, the live site
recovered on its own -- **no Cloudflare DNS/CNAME record needed to be
touched at all**. This means Cloudflare's proxy connects to Render via a
stable edge, and Render internally routes each request by matching its Host
header against whichever service currently has that domain verified,
independent of the literal `*.onrender.com` CNAME target Render's own "View
DNS details" dialog displays. (This is inferred from observed behavior, not
confirmed from Render/Cloudflare documentation -- flagged as a guess, not a
fact, per the same labeling discipline as everything else in this file.)

**Total outage window**: on the order of a few minutes -- from removing the
domain from the old service to the new service's certificate finishing
issuance and traffic resuming.

Confirmed recovered by direct check: `Server: cloudflare`,
`x-render-origin-server: SimpleHTTP/0.6 Python/3.12.3` (proving the new
Docker service, whose base image ships Python 3.12, was now the origin --
the old native-runtime service pinned 3.11.9), `/api/orca_status`
`available: true`. Followed by a full validation on the live domain itself:
viewer page 200, a real hybrid `/api/generate` request 200 with `M83`
confirmed present, 17.0s.

### 11.4 Old service: suspended, not deleted

`Non-Planar-Slicer` (the original native-Python service) was suspended
(Settings -> Delete or suspend -> Suspend Web Service), not deleted --
kept as an instant fallback: if the new Docker service ever needs to be
rolled back, resuming the old one and re-adding the domain to it is a much
faster recovery than recreating a service from scratch. It currently costs
nothing while suspended and holds no traffic.

## 12. Current state (post-deployment)

| Service | Render service ID | Runtime | Status | Serves the domain? |
|---|---|---|---|---|
| `Non-Planar-Slicer-Docker` | `srv-dadsmiou01pc73c5esi0` | Docker | Live | Yes -- `trident.mmucybertron.com` |
| `Non-Planar-Slicer` | `srv-d9njnpqjnfac73b7rg8g` | Python 3 (native) | Suspended | No (fallback only) |

`render.yaml` in the repo now describes the *intended* configuration for
`Non-Planar-Slicer-Docker` (region, plan, env vars) but is **not** wired to
it via Render's Blueprint sync -- it was configured by hand through the
dashboard, matching `render.yaml` field-for-field, for the reasons in
Section 11.1. If `render.yaml` is edited again later, remember it will not
auto-apply to `Non-Planar-Slicer-Docker` either, for the same platform
reason described in Section 11.

### Rollback, if ever needed

1. Render dashboard -> `Non-Planar-Slicer` -> Settings -> Delete or suspend
   -> Resume Web Service.
2. Remove `trident.mmucybertron.com` from `Non-Planar-Slicer-Docker`'s
   Custom Domains, then add it to `Non-Planar-Slicer`'s. Expect the same
   brief outage pattern as Section 11.3 (Render's one-domain-per-service
   rule applies either direction).
3. `git revert` is optional/cosmetic at that point -- the two live services
   are the actual source of truth for what's deployed, not `render.yaml`,
   given Section 11's finding that it does not drive an existing service.
