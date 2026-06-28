# memory mosh — desktop (real datamoshing)

This is the authentic version of the effect: it doesn't simulate
datamoshing on canvas, it does the actual thing — removing real I-frames
(keyframes) from the encoded video so the decoder is forced to keep
applying later frames' motion vectors to a stale reference image, and
duplicating real delta (P) frames so motion vectors get applied
repeatedly, dragging content further each time.

This only works as a desktop tool because it needs to operate on the raw
encoded bitstream, which browsers don't expose. It uses **ffmpeg** —
free, open source, and notably permissive about decoding corrupted
streams (which is exactly why classic desktop datamosh tools use it).

## How it works

1. Transcodes your input to AVI using ffmpeg's `mpeg4` codec with a
   near-infinite keyframe interval, so most of the video is one long run
   of delta frames.
2. Parses the AVI container directly (`avimosh.py`) — reads the `idx1`
   frame index to tell real keyframes from delta frames, then rewrites
   the frame list: keyframes after the first are removed, some delta
   frames are duplicated 2-4x, and occasionally a frame freezes and
   drags for much longer.
3. Re-encodes the moshed AVI through ffmpeg into a normal MP4/WebM.
   ffmpeg's own decoder renders the corruption rather than erroring on
   the missing keyframes, so the glitch gets baked into the pixels and
   the result plays back normally everywhere.

This has been tested end-to-end in this repo: splicing two very
different clips together, removing the keyframe at the cut, and
confirming the second clip's content visibly bleeds through the first
clip's stale reference frame with real motion-compensation block
artifacts — then confirming that corruption survives a full re-encode to
a standard, shareable MP4.

## Usage

Requires Python 3 and ffmpeg on your PATH. No pip dependencies.

```bash
python3 memory_mosh.py input.mp4 output.mp4
```

Options:

```
--keyframe-removal-rate FLOAT   probability a keyframe after the first is removed (default 0.9)
--duplicate-rate FLOAT          probability a delta frame is duplicated a few times (default 0.15)
--duplicate-min / --duplicate-max   how many times (default 2-4)
--freeze-chance FLOAT           probability of a much longer freeze/drag (default 0.02)
--freeze-min / --freeze-max     how many frames the freeze lasts (default 6-18)
--quality INT                   intermediate encode quality, 1=best 31=worst (default 3)
--seed INT                      fix the random seed for reproducible results
--keep-intermediate             keep the raw and moshed .avi files for inspection
```

Cranking `--keyframe-removal-rate` toward 1.0 and `--duplicate-rate` /
`--freeze-chance` up will push the effect harder. A clip with at least
one real scene cut (so there's a keyframe sitting at that cut to remove)
tends to produce the most dramatic results — that's the moment one
scene's motion data gets applied to the previous scene's image.

## Relationship to the browser app

The `../index.html` browser app simulates this look at the pixel level so
it can run anywhere, including as an always-on ambient loop on a website.
This desktop tool does the real thing on the actual compressed video, at
the cost of needing ffmpeg and a desktop environment. Use the browser
app for the deployable, loopable "fuzzy memory" piece; use this for
when you want the authentic corruption itself.

## Status / next steps

This is a working first version focused on validating the real
mechanism. Not yet built:
- Tying keyframe-removal/duplication rates to the "vividness curve"
  concept from the browser app, so the real moshing responds to motion/
  audio the same way.
- A simple GUI instead of the command line.
- Support for input formats ffmpeg's `mpeg4` encoder struggles with
  (very high resolutions in particular will be slow at the intermediate
  step — this hasn't been tuned for hour-long source yet).
