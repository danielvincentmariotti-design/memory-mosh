# memory mosh — project handoff

This doc exists so a fresh conversation (Claude Code, a collaborator,
future-you) has full context without needing the original chat history.
It's rewritten from scratch here — the project has moved a long way past
the version this doc used to describe. Drop this in the project root
(it already lives there: `D:\Memory Mosh\PROJECT_HANDOFF.md`).

## The idea

A datamoshing tool built for one specific artistic purpose: make a video
feel like a fuzzy, half-remembered, loopable memory. Free and open
source throughout — no paid services, no API keys, ever. The user
explicitly does not want this to "get too big too quickly" — prefers
incremental scope over a sprawling feature set, but has been steadily
growing it in well-tested increments over many sessions.

**The browser-app idea from early on never got built.** Everything that
exists is the desktop Python tool. Don't assume `index.html`/`app.js`
exist — they don't.

## Where the code lives (all at repo root, not under `desktop/`)

- `memory_mosh.py` — the GUI (Tkinter) + CLI + `run_pipeline`/
  `run_pipeline_auto` orchestration. This is the file that changes most.
- `avimosh.py` — the real bitstream-level datamosh engine (AVI/mpeg4
  container surgery: keyframe removal, delta-frame duplication). Zero
  dependencies beyond stdlib. The highest-risk-to-get-wrong file in the
  project — raw binary format parsing.
- `pixelsort.py` — threshold-interval pixel sorting as a post-process
  pass over decoded frames. Needs `pillow` + `numpy` (installed in the
  project's `.venv`) — the only part of the pipeline that isn't
  dependency-free.
- `subject_protect.py` — HSL-based compositing that keeps a color range
  playing from the clean source while everything else moshes. Shares
  pixelsort's dependency.
- `styles.css` — a small custom CSS-like file the GUI parses itself for
  font styling (see `_load_css_rules`). Not real CSS, don't expect a
  browser to render it.
- `spike/` — gitignored scratch/test directory. Throwaway spike scripts
  and comparison renders live here during development; nothing in it is
  real project code.

Git: this is now a real GitHub repo
(`github.com/danielvincentmariotti-design/memory-mosh`), branch-per-
feature workflow, merged into `main` once the user has validated a
feature actually works (not just that it runs without error).

## How the real mosh mechanism works (avimosh.py)

1. Transcode input to AVI/mpeg4 with a *configurable* keyframe interval
   (`-g`, default effectively-infinite unless "Force periodic keyframes"
   is on — see below).
2. Parse the AVI container directly — `idx1` frame index tells real
   keyframes from delta frames.
3. Mosh: keyframes after the first get removed with some probability
   (real I-frame removal — decoder then applies later motion vectors to
   a stale reference image, which is *why* removed-keyframe moments make
   content visibly relocate to the wrong part of the frame, not just
   freeze). Delta frames get duplicated 2-4x (or a longer freeze) with
   some probability — this is the actual motion-vector-dragging effect.
4. Rebuild the AVI byte-for-byte (recomputed `idx1`, patched
   `dwTotalFrames`/`dwLength`).
5. Re-encode through ffmpeg into a normal MP4/WebM — the permissive
   decoder bakes the corruption into pixels, so output plays everywhere.

**Keyframe interval matters a lot creatively.** With the old fixed
`-g 9999`, a single continuous shot with no real scene cuts has almost
no keyframes to remove, so nearly all visible glitching came from
delta-frame duplication (stutter-in-place). Lowering the interval
(GUI: check "Force periodic keyframes", then set "Keyframe interval",
try 15-60) forces periodic keyframes even on static footage, giving the
relocation-glitch effect real material to work with. Validated with a
real before/after comparison against an actual scene cut in test
footage — dramatic, confirmed difference.

## The full pipeline, in order (`run_pipeline` in memory_mosh.py)

1. Transcode to raw AVI/mpeg4 (configurable keyframe interval).
2. Vividness curve analysis (optional, on by default) — see below.
3. Mosh (`avimosh.mosh_file`).
4. Re-encode to MP4/WebM.
5. **Frame-effects stage** (optional) — subject-protect and pixel-sort
   (when pixel-sort runs *after* mosh, which is the default) are merged
   into a single extract-once → apply-in-place → reassemble-once pass,
   rather than each doing its own full video encode/decode round trip.
   Subject-protect always runs here (it needs the moshed output to
   composite against). Pixel-sort *can* instead run *before* the mosh
   (see "Pixel-sort timing" below) — in that case it happens earlier,
   on the clean source, before step 1.
6. Smoothing (optional) — motion-aware `minterpolate`, not a blur; runs
   last.

All of this is wrapped by `run_pipeline_auto`, which is what the GUI
and CLI actually call (see "Long-form segmentation" below) — it decides
whether the source needs splitting into chunks first.

## Key concepts and where they live

### Vividness curve (`build_vividness_curve`)
A sine wave (loop-safe by default) blended with real motion-energy
analysis (cheap grayscale frame-diffing, no ML) and optional audio
energy. Low vividness → more corruption; high vividness → mostly clean.
Drives `avimosh.rate_from_curve` scaling of keyframe-removal/duplicate/
freeze rates. **Known asymmetry, not yet resolved**: duplicate_rate and
freeze_chance are *always* passed through the vividness-scaling formula
(even with the curve off, via a flat 0.5 default), but
`keyframe_removal_rate` is applied raw/unscaled always. There's a dead-
code trail (an unused `keyframe_rate` variable, since removed) that
suggests someone meant to fix this and didn't. Flagged to the user,
deliberately left as-is pending a decision — don't silently "fix" it.

### Forgetting curve (`forgetting_curve_retention`, `build_forgetting_curve`)
A genuine power-law retention model from the actual forgetting-curve
literature (`b = 100k / ((log t)^c + k)`), not a placeholder — the user
specifically asked for this formula. Resets to 1.0 (freshly remembered)
at the start of every `cycles` cycle and decays across it — same
cyclical rhythm as the vividness curve, different shape. Drives
pixel-sort's aggression (`pixelsort.aggression_from_curve`, reusing
`avimosh.rate_from_curve`'s decay shape). Both curve builders accept
`t_offset`/`t_span`/`close_loop` params so a long-form render's chunks
can continue the curve phase across chunk boundaries instead of
resetting each chunk to its own fresh cycle — see segmentation below.

### Pixel sort (`pixelsort.py`)
Threshold-interval sort: pixels whose value on a channel
(brightness/hue/saturation/lightness) falls in a window get sorted
along rows/cols/both. **Direction** (rows/cols/both), **Sort key**
(which channel), **Aggression** (window width, curve-driven ceiling),
**Timing** (`after-mosh` default — sort and mosh stay separate legible
layers; `before-mosh` — sorted footage gets moshed too, fuses into one
rougher painterly texture, user confirmed preference for `after-mosh`
via a real A/B comparison but both are exposed). Multiprocessing across
CPU cores — this was a real perf problem early on (serial Python loop,
~0.27s/frame) before parallelizing; now genuinely fast. Intermediate
frames are JPEG (`-q:v 2`/quality 92), not BMP — BMP was blowing past
20GB of temp disk on long clips.

### Subject protect (`subject_protect.py`)
HSL-based "protect this color, not remove it" — inverse chroma-key.
Pixels in the Low-High range (measured on the *clean* source) play
forward at **normal speed, sequential clean-source frame per output
frame** — deliberately *not* synced to the mosh's own duplicate/freeze
timeline, since that was an earlier bug (protected region would freeze
right along with everything else). Has a GUI reference table (tone/hue
bands) since a raw color threshold has no concept of "this is a face" —
it'll grab any pixel in range regardless of shape, which burned the user
once on busy footage (looked like disconnected color blobs, not a
protected subject). Works best on simple, well-separated footage.

### Smoothing (`apply_temporal_blend`)
Runs last. Started as `tmix` (does nothing on duplicate/frozen frames —
averaging a frame with copies of itself is a no-op, which is exactly
where the stutter you'd want smoothed actually is). Now ffmpeg
`minterpolate` — motion-aware, genuinely fills duplicate/frozen
stretches. `blend` mode gentler, `motion` mode stronger but more prone
to warping around the harshest jumps.

### Themes
`dark`, `ember` (default), `vaporwave` (neon-bordered sections via a
`section_border` palette key), `scooby` (Mystery-Machine palette),
`win95` (replaced a near-duplicate `light` theme). Real bug fixed here:
`styles.css` used to hardcode a dark-theme text color that silently
overrode every theme's own header/label color — that's what made
headers unreadable on light backgrounds, not anything theme-specific.

## Long-form segmentation (the newest, biggest piece — see below for status)

**The problem**: ffmpeg splits the mpeg4/AVI intermediate into multiple
top-level `RIFF`/`AVIX` segments (OpenDML "AVI 2.0") once it passes
roughly 1GB — common on long and/or high-resolution sources.
`avimosh.py` only ever read the first segment. Fixed in two layers:

1. A real bug fix: an OpenDML `ix00` index chunk inside `movi` was being
   miscounted as a video frame (off-by-one frame-count mismatch error).
   Fixed by filtering to real stream-data chunks only (`00dc`/`00db`
   tags).
2. A safety net: `avimosh.parse_avi` now detects a multi-segment file
   and raises a clear error instead of silently processing only the
   first ~20% of the video (the failure mode *before* this fix — worse
   than a crash, since it produced a plausible-looking but wrong output
   with no warning).

**The actual solution** (`run_pipeline_auto`, `estimate_segment_plan`,
`_run_segmented` in memory_mosh.py): estimate the intermediate size up
front by test-encoding a real 10-second sample with the exact settings
that will be used (not a generic resolution/duration guess), and if it
would land past the ceiling, transparently split the source into chunks
(ffmpeg segment muxer, stream-copy, no re-encode), run each chunk
through the *unmodified* `run_pipeline`, and stitch the results back
together (ffmpeg concat, also stream-copy). A normal-sized clip takes
the exact same single-pass path as before — confirmed identical, not
just similar (no `segmented` key in the result dict at all when
segmentation didn't trigger).

Per-chunk seeds derive from the base seed (`seed + chunk_index`) instead
of reusing one seed identically across chunks. Vividness/forgetting
curves get a phase offset/span per chunk so they continue smoothly
across chunk boundaries instead of each chunk restarting its own cycle
— validated the phase math reproduces a single continuous pass almost
exactly.

**Validated end-to-end on real footage** (forced a 56s clip into 3
chunks, mosh + pixel-sort both on): split correctly, each chunk
processed correctly, progress reporting stayed monotonic across all
three, stats aggregated correctly, stitched output played back clean —
pulled frames from multiple points, no seam artifacts.

**Not yet validated**: a real long-form render (30+ minutes) start to
finish. This is exactly what triggered the whole feature — the user hit
the OpenDML crash on a real 34-minute, 1920x1080 source (5.15GB raw AVI
intermediate, 5 RIFF segments) and, separately, successfully worked
around it manually by raising `--quality` (more compression) to keep a
single-pass intermediate under 1GB (took ~8 hours for that one render).
**The user was about to test the same 33-minute source overnight
against the new automatic segmentation feature when this handoff was
written — check with them for the result before doing anything else
with this feature.** If it worked: merge `feature/long-form-
segmentation` into `main`. If it didn't: get the actual failure mode
before guessing at a fix.

**Known gaps in the segmentation feature** (v1, not yet hardened):
- If a chunk's *own* intermediate still lands past the ceiling despite
  targeting comfortably under it (700MB target vs ~1GB ceiling, content
  complexity varies), `_run_segmented` will raise partway through after
  some chunks already succeeded — no automatic re-chunking/retry.
- Chunk boundaries come from stream-copy splitting, which can only cut
  at source keyframes — actual chunk durations vary from the target,
  usually not by much.
- No manual override UI for chunk count/duration — fully automatic based
  on the size estimate.

## Current git state (branches)

- `main` — has everything through: pixel-sort, subject-protect,
  keyframe-interval control, pixel-sort timing, the AVI multi-segment
  crash fix + safety net, the intermediate-size warning, and the theme
  overhaul.
- `feature/long-form-segmentation` — the automatic segmentation feature
  described above. Committed, pushed, **not yet merged** — pending the
  user's overnight real-world test.
- Older feature branches (`feature/pixel-sort`,
  `feature/subject-protection`, `feature/keyframe-interval`) still exist
  on `origin` for reference but are fully merged into `main`; no need to
  touch them again.

## How this user likes to work (carries across sessions)

- Wants a new git branch for any real feature work, never directly on
  `main`. Merges only after they've personally validated a feature
  works — not just that it ran without error.
- Consistently wants things *tested*, not just written: render real
  output, extract frames, look at them, compare before/after. Several
  features in this project only reached their current form because an
  initial version was tested against real footage and turned out to
  look wrong (subject-protect's color-blob problem, tmix doing nothing
  on duplicate frames, the frozen-clean-region bug).
- Likes concrete before/after comparisons when proposing a design
  change (e.g. mosh-then-sort vs sort-then-mosh, tmix vs minterpolate) —
  offer to spike and compare rather than just implementing one option.
- Prefers GUI description text tight and to the point — an earlier pass
  of this session's own descriptions was flagged as too wordy and
  rewritten shorter.
- Interested in genuinely understanding mechanisms (asked for and got a
  real technical walkthrough of the vividness curve's math, the OpenDML
  binary index format, etc.) — don't oversimplify explanations for this
  user.
- Likes 90s-ish/nostalgic aesthetic touches (vaporwave, Scooby-Doo,
  Windows 95 themes all came from direct requests).

## Constraints to keep in mind

- Everything free/open source — no paid services, no API keys, ever.
- Don't let scope balloon — this user has said so explicitly more than
  once, even while steadily growing the feature set. Ship one tested
  thing at a time.
- The core mosh pipeline has no pip dependencies; pixel-sort and
  subject-protect are the one accepted exception (pillow + numpy,
  installed in `.venv`) — don't casually add more dependencies.
