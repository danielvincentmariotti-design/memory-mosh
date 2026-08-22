"""
subject_protect.py — HSL-based clean/moshed compositing.

Splits each output frame between two sources based on a hue/saturation/
lightness/brightness threshold: pixels inside the protected range are
pulled from the ORIGINAL, uncorrupted source — playing forward at normal
speed, sequential frame N of the clean source for output frame N — while
everywhere else comes from the moshed output, so it freezes/drags/
glitches as usual. Picture a chroma-key, but keying on "protect this
color" instead of "remove this color."

Deliberately does NOT try to line the clean frame up with "whichever
original frame the mosh happened to be dragging at this moshed frame
position" — that would make the protected region inherit the mosh's own
stutter (freezing right along with everything else whenever a frame gets
duplicated), which defeats the point. The protected region is meant to
read as independent of the glitch timeline, not synced to it.

Optional feature — shares pixelsort.py's pillow + numpy dependency.
"""

import multiprocessing as mp
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from pixelsort import (
    PIXELSORT_AVAILABLE as SUBJECT_PROTECT_AVAILABLE,
    hue_channel, saturation_channel, lightness_channel, luminance,
    extract_frames, reassemble_frames,
)

if SUBJECT_PROTECT_AVAILABLE:
    import numpy as np
    import PIL.Image

CHANNELS = {
    'brightness': luminance,
    'hue': hue_channel,
    'saturation': saturation_channel,
    'lightness': lightness_channel,
}


def _run_ffmpeg(args, label):
    cmd = ['ffmpeg', '-y', '-nostdin', *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or '').strip()
        preview = details[-2000:] if details else 'No ffmpeg output was returned.'
        raise RuntimeError(f'ffmpeg failed during {label}: {preview}')


def _probe_fps(path):
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return 30.0
    result = subprocess.run(
        [ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=r_frame_rate',
         '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
        capture_output=True, text=True,
    )
    value = result.stdout.strip()
    if not value:
        return 30.0
    if '/' in value:
        num, den = value.split('/')
        den = float(den)
        return float(num) / den if den else 30.0
    return float(value)


def _process_one_frame(task):
    moshed_path, clean_path, out_path, channel, low, high = task
    moshed = np.array(PIL.Image.open(moshed_path).convert('RGB'))
    clean = np.array(PIL.Image.open(clean_path).convert('RGB'))
    key_fn = CHANNELS.get(channel, hue_channel)
    key_values = key_fn(clean)
    mask = (key_values >= low) & (key_values <= high)
    composite = np.where(mask[..., None], clean, moshed)
    PIL.Image.fromarray(composite.astype(np.uint8)).save(out_path, quality=92)


def composite_frames_in_place(moshed_frame_paths, clean_frame_paths,
                               channel='hue', low=0.0, high=60.0,
                               progress_callback=None, progress_start=0.0, progress_end=1.0):
    """Composites each moshed frame file against a clean frame and
    overwrites the moshed file in place — lets a caller chain this with
    another in-place frame stage (e.g. pixel sort) without an extra
    encode/decode round trip between them.

    Deliberately uses sequential position (moshed frame j <-> clean frame
    j), NOT a mapping back to which original frame the mosh duplicated/
    dropped — the protected region should play the source forward at
    normal speed, decoupled from the mosh's own stutter/freeze timeline,
    not inherit it. Once the clean source runs out (the mosh duplicated
    its way to a longer output than the original), it holds on the last
    clean frame rather than looping or erroring."""
    total = len(moshed_frame_paths)
    if total == 0:
        return

    worker_count = max(1, (os.cpu_count() or 2) - 1)
    if progress_callback:
        progress_callback(
            f'Subject protect: compositing {total} frames across {worker_count} worker processes…',
            progress_start)

    tasks = []
    for j in range(total):
        clean_idx = min(j, len(clean_frame_paths) - 1)
        tasks.append((
            str(moshed_frame_paths[j]), str(clean_frame_paths[clean_idx]),
            str(moshed_frame_paths[j]), channel, low, high,
        ))

    t0 = time.time()
    completed = 0
    with mp.Pool(processes=worker_count) as pool:
        for _ in pool.imap_unordered(_process_one_frame, tasks, chunksize=4):
            completed += 1
            if progress_callback and (completed % 10 == 0 or completed == total):
                elapsed = time.time() - t0
                rate = completed / elapsed if elapsed > 0 else 0
                remaining = (total - completed) / rate if rate > 0 else 0
                progress_callback(
                    f'Subject protect: {completed}/{total} frames (~{remaining:.0f}s remaining)',
                    progress_start + (progress_end - progress_start) * (completed / total),
                )


def composite_subject_protect(moshed_path, clean_source_path, output_path,
                               channel='hue', low=0.0, high=60.0,
                               temp_root=None, progress_callback=None):
    """Standalone convenience wrapper: extract both sides -> composite in
    place -> reassemble. For chaining with pixel-sort without a redundant
    encode/decode round trip, use extract_frames / composite_frames_in_place
    / reassemble_frames directly instead."""
    if not SUBJECT_PROTECT_AVAILABLE:
        raise RuntimeError(
            'Subject protect requires the "pillow" and "numpy" packages, which are not installed. '
            'Install with: pip install pillow numpy'
        )

    moshed_path = Path(moshed_path)
    clean_source_path = Path(clean_source_path)
    output_path = Path(output_path)

    if temp_root:
        Path(temp_root).mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix='memory-mosh-protect-', dir=str(temp_root) if temp_root else None))
    moshed_frames_dir = workdir / 'moshed'
    clean_frames_dir = workdir / 'clean'

    try:
        fps = _probe_fps(moshed_path)

        if progress_callback:
            progress_callback('Subject protect: extracting moshed frames…', 0.0)
        moshed_frame_paths = extract_frames(moshed_path, moshed_frames_dir, label='protect-extract-moshed')

        if progress_callback:
            progress_callback('Subject protect: extracting clean source frames…', 0.05)
        clean_frame_paths = extract_frames(clean_source_path, clean_frames_dir, label='protect-extract-clean')

        composite_frames_in_place(moshed_frame_paths, clean_frame_paths,
                                   channel=channel, low=low, high=high,
                                   progress_callback=progress_callback, progress_start=0.1, progress_end=0.9)

        if progress_callback:
            progress_callback('Subject protect: re-assembling video…', 0.95)
        reassemble_frames(moshed_frames_dir, fps, output_path, label='protect-reassemble')
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
