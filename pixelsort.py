"""
pixelsort.py — threshold-interval pixel sorting, applied as a post-process
pass over decoded frames.

This is a different kind of corruption than avimosh.py's real bitstream
datamoshing: it operates on decoded pixels rather than the encoded stream,
so it needs to fully decode to frames and re-encode. Kept as an optional
pass (see run_pipeline's 'pixel_sort_enabled' config flag) rather than
merged into the AVI-level mosh.

Optional feature — requires pillow + numpy (unlike the core mosh pipeline,
which has no pip dependencies). Import failures are caught so the rest of
the app works fine without them; pixel_sort_video() raises a clear error
if actually called without the deps installed.

Aggression can be driven by a per-frame decay curve (see
memory_mosh.build_forgetting_curve) rather than a single flat value, so
'forgotten' stretches of the clip dissolve harder than well-retained ones.
"""

import multiprocessing as mp
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

try:
    import numpy as np
    import PIL.Image
    PIXELSORT_AVAILABLE = True
except ImportError:
    PIXELSORT_AVAILABLE = False


def _run_ffmpeg(args, label):
    cmd = ['ffmpeg', '-y', '-nostdin', *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or '').strip()
        preview = details[-2000:] if details else 'No ffmpeg output was returned.'
        raise RuntimeError(f'ffmpeg failed during {label}: {preview}')


def _resample_series(series, length):
    if not series:
        return [0.0] * length
    if len(series) == length:
        return [float(v) for v in series]
    return [float(series[min(int(i * len(series) / length), len(series) - 1)]) for i in range(length)]


def aggression_from_curve(base_aggression, retention, floor=0.02, strength=0.85):
    """Scale aggression down as retention rises — same decay shape avimosh.py
    uses for keyframe/duplicate rates (rate_from_curve), applied here so a
    low-retention ('forgotten') stretch of the clip dissolves harder under
    the pixel sort than a well-retained one. `retention` is expected to come
    from memory_mosh.build_forgetting_curve (0-1, 1 = freshly remembered)."""
    vibe = max(0.0, min(1.0, retention))
    return max(floor, base_aggression * (1.0 - vibe * strength))


def _probe_fps(path):
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=r_frame_rate',
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


def luminance(pixels):
    r = pixels[..., 0].astype(np.float32)
    g = pixels[..., 1].astype(np.float32)
    b = pixels[..., 2].astype(np.float32)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _rgb_max_min(pixels):
    r = pixels[..., 0].astype(np.float32)
    g = pixels[..., 1].astype(np.float32)
    b = pixels[..., 2].astype(np.float32)
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    return r, g, b, maxc, minc


def hue_channel(pixels):
    """Hue angle, scaled from [0, 360) degrees to a 0-255 range so it lines
    up with the same threshold/aggression math as the other keys."""
    r, g, b, maxc, minc = _rgb_max_min(pixels)
    delta = maxc - minc
    delta_safe = np.where(delta == 0, 1.0, delta)
    hue = np.zeros_like(maxc)
    hue = np.where((maxc == r) & (delta != 0), ((g - b) / delta_safe) % 6, hue)
    hue = np.where((maxc == g) & (delta != 0), ((b - r) / delta_safe) + 2, hue)
    hue = np.where((maxc == b) & (delta != 0), ((r - g) / delta_safe) + 4, hue)
    hue = (hue * 60.0) % 360.0
    return hue / 360.0 * 255.0


def saturation_channel(pixels):
    """HSV saturation (how vivid vs. washed-out a pixel is), scaled 0-255."""
    _, _, _, maxc, minc = _rgb_max_min(pixels)
    delta = maxc - minc
    maxc_safe = np.where(maxc == 0, 1.0, maxc)
    sat = np.where(maxc == 0, 0.0, delta / maxc_safe)
    return sat * 255.0


def lightness_channel(pixels):
    """HSL lightness — (max+min)/2, a color-neutral brightness measure
    distinct from luminance's perceptual (green-weighted) brightness."""
    _, _, _, maxc, minc = _rgb_max_min(pixels)
    return (maxc + minc) / 2.0


SORT_KEYS = {
    'brightness': luminance,
    'hue': hue_channel,
    'saturation': saturation_channel,
    'lightness': lightness_channel,
}


def sort_key_values(img_array, key='brightness'):
    return SORT_KEYS.get(key, luminance)(img_array)


def aggression_to_threshold(aggression, center=128.0, min_width=20.0, max_width=235.0):
    """aggression in [0, 1] -> (low, high) brightness window around center.
    0 = barely any pixels eligible to sort, 1 = almost the whole frame."""
    width = min_width + (max_width - min_width) * aggression
    return max(0.0, center - width / 2), min(255.0, center + width / 2)


def _sort_line(line, mask, key):
    out = line.copy()
    n = len(mask)
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < n and mask[j]:
            j += 1
        order = np.argsort(key[i:j])
        out[i:j] = line[i:j][order]
        i = j
    return out


def sort_rows(img_array, low, high, key='brightness'):
    key_values = sort_key_values(img_array, key)
    mask = (key_values >= low) & (key_values <= high)
    out = img_array.copy()
    for y in range(img_array.shape[0]):
        out[y] = _sort_line(img_array[y], mask[y], key_values[y])
    return out


def sort_cols(img_array, low, high, key='brightness'):
    key_values = sort_key_values(img_array, key)
    mask = (key_values >= low) & (key_values <= high)
    out = img_array.copy()
    for x in range(img_array.shape[1]):
        out[:, x] = _sort_line(img_array[:, x], mask[:, x], key_values[:, x])
    return out


def pixel_sort_frame(img_array, direction, aggression, key='brightness'):
    low, high = aggression_to_threshold(aggression)
    if direction == 'rows':
        return sort_rows(img_array, low, high, key=key)
    if direction == 'cols':
        return sort_cols(img_array, low, high, key=key)
    # 'both' — compound: sort rows, then sort the result's columns
    return sort_cols(sort_rows(img_array, low, high, key=key), low, high, key=key)


def _process_one_frame(task):
    frame_path, out_path, direction, aggression, key = task
    arr = np.array(PIL.Image.open(frame_path).convert('RGB'))
    sorted_arr = pixel_sort_frame(arr, direction, aggression, key=key)
    PIL.Image.fromarray(sorted_arr).save(out_path)


def pixel_sort_video(input_path, output_path, direction='both', aggression=0.5, key='brightness',
                      temp_root=None, progress_callback=None, decay_curve=None):
    if not PIXELSORT_AVAILABLE:
        raise RuntimeError(
            'Pixel sort requires the "pillow" and "numpy" packages, which are not installed. '
            'Install with: pip install pillow numpy'
        )

    input_path = Path(input_path)
    output_path = Path(output_path)

    if temp_root:
        Path(temp_root).mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix='memory-mosh-pixelsort-', dir=str(temp_root) if temp_root else None))
    frames_dir = workdir / 'frames'
    sorted_dir = workdir / 'sorted'
    frames_dir.mkdir(parents=True)
    sorted_dir.mkdir(parents=True)

    try:
        fps = _probe_fps(input_path)

        if progress_callback:
            progress_callback('Pixel sort: extracting frames (uncompressed — fast but uses temp disk space)…', 0.0)
        # .bmp instead of .png: no compression cost on either the ffmpeg write
        # or the PIL read, which matters a lot once you're doing it per-frame
        # across a whole clip.
        _run_ffmpeg(['-i', str(input_path), str(frames_dir / 'f%06d.bmp')], 'pixel-sort-extract')

        frame_paths = sorted(frames_dir.glob('*.bmp'))
        total = len(frame_paths)

        if decay_curve:
            per_frame_curve = _resample_series(decay_curve, total)
            per_frame_aggression = [aggression_from_curve(aggression, v) for v in per_frame_curve]
        else:
            per_frame_aggression = [aggression] * total

        worker_count = max(1, (os.cpu_count() or 2) - 1)
        if progress_callback:
            progress_callback(f'Pixel sort: sorting {total} frames across {worker_count} worker processes…', 0.05)

        tasks = [
            (str(frame_paths[i]), str(sorted_dir / frame_paths[i].name), direction, per_frame_aggression[i], key)
            for i in range(total)
        ]
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
                        f'Pixel sort: {completed}/{total} frames '
                        f'(~{remaining:.0f}s remaining)',
                        0.05 + 0.85 * (completed / total),
                    )

        if progress_callback:
            progress_callback('Pixel sort: re-assembling video…', 0.95)
        if output_path.suffix.lower() == '.webm':
            codec_args = ['-c:v', 'libvpx-vp9', '-crf', '30', '-b:v', '0']
        else:
            codec_args = ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18']
        _run_ffmpeg(
            ['-framerate', str(fps), '-i', str(sorted_dir / 'f%06d.bmp'), *codec_args, str(output_path)],
            'pixel-sort-reassemble',
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
