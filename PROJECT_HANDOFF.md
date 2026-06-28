# memory mosh — project handoff

This doc exists so a fresh conversation (Claude Code, a collaborator,
future-you) has full context without needing the original chat history.
Drop this in the project root.

## The idea

A datamoshing tool, but built for one specific artistic purpose: make a
video feel like a fuzzy, half-remembered, loopable memory — meant to
play all day as ambient art, not be watched start-to-finish. Free and
open source throughout; no paid services, no API keys.

Two separate tools have come out of this, covering two different
versions of "datamoshing":

1. Browser app (`index.html`, `app.js`, `style.css`) — simulates the
datamosh look at the pixel level, runs anywhere (deployable to a
website, works offline once built out as a PWA). Good for the
always-on ambient loop use case.
2. Desktop tool (`desktop/avimosh.py`, `desktop/memory_mosh.py`) —
does real datamoshing on the actual video bitstream (genuine
I-frame removal, genuine delta-frame duplication). Only possible on
desktop because it needs raw bitstream access, which browsers don't
expose. This is the authentic version of the effect.

These are not yet connected to each other. A live design question (see
"next steps") is whether the desktop tool's intensity should be driven
by the same "vividness curve" concept the browser app uses.

---

## Part 1: Browser app

### Concept

- A single vividness curve spans the whole clip — a value from 0
(hazy/decayed) to 1 (vivid) at every point in time. Built from a
loop-safe sine wave (so it returns to the same value at the end as the
start — necessary for looping) blended with real motion-energy
analysis (cheap frame-diffing, no ML) so vividness is biased toward
moments with actual motion in the source.
- Low vividness → more corruption. High vividness → mostly clean. This
ties the "feels like a fading memory" emotional read directly to a
single tunable curve.
- Memory intrusions: occasionally cuts away to a more-vivid moment
pulled from elsewhere in the clip — a short flash, then back — rather
than playing strictly in chronological order.
- Loop-melt: the last ~1.5s crossfades into the first ~1.5s so the
exported clip loops with no hard seam — deliberately leaning into the
seam as a melt moment rather than hiding it.

### How the corruption is actually rendered (this evolved a few times)

This is a simulation, not real datamoshing — it estimates motion
itself and fakes the look on a `<canvas>`. Current approach (v3):

- Block-motion drag: coarse block-matching motion estimation against
the previous frame (downscaled luma diffing, small search window).
At low vividness, instead of drawing the fresh block, it drags STALE
texture from a persistent buffer along that motion vector — the
buffer is never cleared, so this accumulates into real melt/smear
rather than a crossfade. (An earlier version just alpha-blended a few
past frames — that read as "frame rate jumping," not bleeding, per
direct user feedback. The block-drag rewrite fixed that.)
- Freeze & drip: periodically holds entirely on one frame while a
separate trail buffer pushes downward and fades each frame, with the
crisp held frame eroding from the bottom up (alpha gradient mask) to
reveal the trail beneath. Red/blue channels separate vertically as it
melts (classic chromatic/CRT-bleed look), plus a couple of soft
vertical re-draws for motion blur. This was added because "echo
blending" didn't produce real color bleeds — this does.
- Chroma bleed: per-channel pixel offset (R/B sampled from different
x/y positions than G).
- Slit-scan tearing: random horizontal bands drift sideways, decay
over a few frames, respawn — for sideways "spill" artifacts.
- Light grain overlay on top, inversely tied to vividness.

### Controls (sliders in the UI)

- `cycles` — how many vividness rise/fall waves across the clip
- `moshIntensity` (labeled "drag / melt strength") — block-drag
probability multiplier
- `meltSize` — block size in px (default 32, range 8-160; smaller =
finer/more legible corruption, larger = abstract/painterly)
- `dripAmount` — freeze & drip frequency/intensity (single dial)
- `colorBleed` — chroma separation strength
- `intrusion` — memory-intrusion cutaway rate
- toggles for memory intrusions and loop-seam melt

### Known limits (not yet built)

- Only suited to clips of a few minutes, not the hour-long target —
motion analysis and rendering both re-seek the `<video>` element frame
by frame, which doesn't scale to an hour. Needs a `WebCodecs` +
chunked/Web-Worker rewrite for long-form support.
- Vividness is fully automatic (motion + sine curve only) — no manual
override UI yet for promoting/demoting specific moments. (User
explicitly wants "automatic suggestions, manual override" as the end
state, not full auto or full manual.)
- No audio analysis yet (motion only).
- No PWA/offline shell yet — plain static page.
- Output is `.webm` via `MediaRecorder`.
- Not yet tested in an actual browser — written carefully and
syntax-checked, but no live browser testing has happened. Treat as
"first pass to react to."

### Tech stack (all free/open)

WebCodecs (future, for long-form) / `<video>` + Canvas (current),
MediaRecorder for export, vanilla JS, no build step. Fonts via Google
Fonts CDN (Fraunces serif for headings, Inter for UI, JetBrains Mono for
readouts). Palette: warm near-black `#16140F`, faded paper `#D8D2C4`,
dusty rose `#A8776B`, sage `#5C6660`, bone `#F0ECE0` — designed to read
like an old photograph, not a dev tool.

---

## Part 2: Desktop tool (the real thing)

### Why this exists

User specifically asked for the literal mechanism: "when the I-frames
get removed and the delta frames are duplicated." That's not something
the canvas simulation does — it's a property of the actual compressed
bitstream. Browsers don't expose this (WebCodecs `VideoDecoder` is
considered too strict/unpredictable for this — browser-native decoders
tend to hard-error on missing keyframes rather than gracefully
corrupting, unlike classic desktop tools). User agreed to drop the
browser constraint for this piece and go desktop-only.

### How it works

1. Transcode the input to AVI using ffmpeg's `mpeg4` codec with
`-g 9999` (near-infinite keyframe interval) — most of the video
becomes one long run of delta (P) frames.
2. Parse the AVI container directly (`avimosh.py`, zero
dependencies beyond Python stdlib) — reads the `idx1` frame index to
tell real keyframes from delta frames via the `AVIIF_KEYFRAME` flag.
3. Mosh it: keyframes after the first are removed with some
probability (real I-frame removal — the decoder is then forced to
apply later motion vectors to a stale reference image). Delta frames
are duplicated 2-4x with some probability (real motion-vector
dragging), or much longer for an occasional freeze/drag.
4. Rebuild the AVI byte-for-byte: new `movi` chunk list, recomputed
`idx1` (offsets are relative to the `movi` fourcc position — confirmed
empirically against ffmpeg's own output, not guessed), patched
`dwTotalFrames`/`dwLength` in the `avih`/`strh` headers.
5. Re-encode through ffmpeg into a normal MP4/WebM. ffmpeg's own
decoder is permissive — it renders the corruption rather than
erroring on the missing keyframes — so the glitch bakes into the
pixels and the output plays back everywhere normally.

### This has been validated, not just written

Tested in the sandbox before handing off:
- Generated two visually distinct synthetic clips (`testsrc` pattern +
`mandelbrot` fractal), each independently encoded with its own
keyframe, spliced together at the raw frame level to simulate a real
scene cut.
- Confirmed a no-op rebuild round-trip is byte-correct (ffprobe showed
identical frame composition before/after).
- Removed the keyframe sitting at the cut. ffmpeg's decoder did NOT
error — decoded all frames cleanly.
- Visually confirmed real corruption: extracted frames around the
cut showed the mandelbrot fractal structure and motion-compensation
block artifacts bleeding through the testsrc color bars — genuine
datamosh, not a filter effect.
- Confirmed the corruption survives a full re-encode to a standard
MP4 (extracted a frame from the final MP4, pixel-identical corruption
present).
- Ran the packaged CLI end-to-end on a fresh clip (ffmpeg `concat`-based
cut, not the manual splice) — same result, corruption confirmed again.
- User has since run it themselves on Windows (real footage, not the
synthetic test clips) — 12 keyframes removed, 1692 duplicate frames
added out of 3380 original, pipeline completed successfully end to
end. Visual review of that specific output is still pending feedback.

### Usage

No pip dependencies — just Python 3 and ffmpeg on PATH.

```bash
python3 memory_mosh.py input.mp4 output.mp4
```

Options: `--keyframe-removal-rate` (default 0.9), `--duplicate-rate`
(default 0.15), `--duplicate-min`/`--duplicate-max` (default 2-4),
`--freeze-chance` (default 0.02), `--freeze-min`/`--freeze-max` (default
6-18), `--quality` (intermediate encode quality, default 3),
`--seed` (reproducibility), `--keep-intermediate` (keep the raw/moshed
`.avi` files for inspection instead of deleting them).

Clips with an actual scene cut (so there's a keyframe worth removing)
produce the most dramatic results — that's the moment one scene's
motion data gets forced onto the previous scene's stale image.

### Known limits (not yet built)

- Intensity is flat random probability, not tied to the vividness-curve
concept from the browser app — moshing doesn't currently respond to
motion or audio automatically. This is the most likely immediate
next step.
- No GUI — command line only.
- Not yet tuned/tested for hour-long source (intermediate `mpeg4`
transcode step timing/file size unverified at that length).
- Tested on synthetic clips + one real user run; broader format/codec
edge cases (resolution extremes, unusual source codecs, files where
ffmpeg's AVI muxer doesn't write a legacy `idx1` index) haven't been
exercised.

---

## Open design questions / natural next steps

In rough priority order based on where the conversation left off:

1. Tie desktop moshing intensity to motion/vividness — port the
motion-energy analysis concept from the browser app so
keyframe-removal-rate and duplicate-rate scale with actual motion in
the source, instead of being flat constants.
2. User is mid-testing the desktop tool on real footage — waiting on
their reaction to a real run (12 keyframes removed, 1692 duplicates)
before tuning defaults further.
3. Browser app needs actual browser testing — nothing in it has been
run live yet.
4. Manual override UI for the browser app's vividness curve (promote/
demote specific moments).
5. Long-form (hour-long) support for both tools.
6. Eventually: turn this into a real git repo / GitHub project (not done
yet — currently just a local folder on the user's machine,
`D:\Memory Mosh` on Windows).

## Constraints to keep in mind

- Everything must stay free/open source — no paid services, no API
keys, ever.
- User explicitly does not want the project to "get too big too quickly"
— prefers incremental scope over a sprawling feature set.
- Browser app and desktop tool are intentionally kept as separate tools
for separate purposes (deployable ambient loop vs. authentic
corruption) rather than merged into one thing.
