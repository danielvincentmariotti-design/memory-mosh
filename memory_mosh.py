#!/usr/bin/env python3
"""
memory_mosh.py — real datamoshing, desktop edition.

Pipeline:
  1. Transcode the input to AVI/mpeg4 with a configurable keyframe
     interval — near-infinite by default (so the source material is
     mostly one long run of delta frames), lowered to get periodic
     keyframes for keyframe-removal to act on even in a single
     continuous shot with no real scene cuts.
  2. Remove keyframes after the first (real I-frame removal) and
     duplicate selected delta frames (real motion-vector dragging),
     directly on the encoded bitstream — see avimosh.py. Removing a
     keyframe is what makes later delta frames apply their motion
     vectors against a stale/wrong reference image — that's what makes
     content visibly relocate to the wrong part of the frame, not just
     freeze in place the way plain frame duplication does.
  3. Re-encode the moshed AVI through ffmpeg's own (permissive) decoder
     into a normal, shareable MP4/WebM. The corruption is baked into
     the pixels at this point, so the output plays everywhere.

Requires only ffmpeg on PATH. No pip dependencies.
"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TEMP_ROOT = Path(__file__).resolve().parent / 'temp'
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from tkinter import Scrollbar

STYLE_PATH = Path(__file__).with_name('styles.css')
SETTINGS_PATH = Path(__file__).resolve().parent / 'user_settings.json'

import avimosh
import pixelsort
import subject_protect
from avimosh import rate_from_curve

THEMES = {
    'dark': {
        'root_bg': '#121721',
        'panel_bg': '#161c28',
        'section_bg': '#1a2130',
        'text_fg': '#f2ecdf',
        'muted_fg': '#8e99aa',
        'entry_bg': '#1a2232',
        'entry_fg': '#f7f2e8',
        'entry_active_bg': '#1f2940',
        'button_bg': '#354152',
        'button_fg': '#f7ebd2',
        'button_active_bg': '#4b5568',
        'button_active_fg': '#ffffff',
        'check_hover_bg': '#2e3e54',
        'check_hover_fg': '#ffffff',
        'progress_bg': '#202838',
        'progress_fg': '#8cbf8d',
        'log_bg': '#0f141d',
        'log_fg': '#f2ecdf',
        'canvas_bg': '#1b1a18',
        'curve_line': '#4f4b42',
        'curve_peak': '#f0c36d',
        'curve_low': '#8b6f47',
        'forget_curve': '#6fb3d9',
    },
    'ember': {
        'root_bg': '#19131a',
        'panel_bg': '#241d24',
        'section_bg': '#2b232c',
        'text_fg': '#f7eae0',
        'muted_fg': '#a48f87',
        'entry_bg': '#2f252d',
        'entry_fg': '#fff3eb',
        'entry_active_bg': '#372c37',
        'button_bg': '#7a3f3f',
        'button_fg': '#fff6f1',
        'button_active_bg': '#944b4b',
        'button_active_fg': '#ffffff',
        'check_hover_bg': '#55343e',
        'check_hover_fg': '#fff3eb',
        'progress_bg': '#3b2c2f',
        'progress_fg': '#f28f4b',
        'log_bg': '#140f13',
        'log_fg': '#f7eae0',
        'canvas_bg': '#221a1f',
        'curve_line': '#705650',
        'curve_peak': '#f0b25d',
        'curve_low': '#8c5a3d',
        'forget_curve': '#4fb3a6',
    },
    'vaporwave': {
        'root_bg': '#1a1030',
        'panel_bg': '#241640',
        'section_bg': '#2d1b4d',
        'text_fg': '#f2e9ff',
        'muted_fg': '#b39ddb',
        'entry_bg': '#2a1850',
        'entry_fg': '#ffe6fa',
        'entry_active_bg': '#38215f',
        'button_bg': '#5b2a86',
        'button_fg': '#ffe6fa',
        'button_active_bg': '#7c3aad',
        'button_active_fg': '#ffffff',
        'check_hover_bg': '#43266b',
        'check_hover_fg': '#ffffff',
        'progress_bg': '#2d1b4d',
        'progress_fg': '#ff6ec7',
        'log_bg': '#150c28',
        'log_fg': '#f2e9ff',
        'canvas_bg': '#1c1235',
        'curve_line': '#5c4380',
        'curve_peak': '#00e5ff',
        'curve_low': '#ff6ec7',
        'forget_curve': '#ffd23f',
        'section_border': '#ff36d9',
    },
    'scooby': {
        'root_bg': '#0f2b2b',
        'panel_bg': '#153636',
        'section_bg': '#1b4141',
        'text_fg': '#f5e6c8',
        'muted_fg': '#a8c9c2',
        'entry_bg': '#1c4444',
        'entry_fg': '#fff3d9',
        'entry_active_bg': '#245252',
        'button_bg': '#c9762b',
        'button_fg': '#fff3d9',
        'button_active_bg': '#e08a35',
        'button_active_fg': '#ffffff',
        'check_hover_bg': '#2f6b63',
        'check_hover_fg': '#ffffff',
        'progress_bg': '#1b4141',
        'progress_fg': '#7fa832',
        'log_bg': '#0a1f1f',
        'log_fg': '#f5e6c8',
        'canvas_bg': '#123232',
        'curve_line': '#3d6b63',
        'curve_peak': '#e8a33d',
        'curve_low': '#7fa832',
        'forget_curve': '#d6547a',
        'section_border': '#2ee6c9',
    },
    'win95': {
        'root_bg': '#c0c0c0',
        'panel_bg': '#d4d0c8',
        'section_bg': '#ece9d8',
        'text_fg': '#000000',
        'muted_fg': '#4a4a4a',
        'entry_bg': '#ffffff',
        'entry_fg': '#000000',
        'entry_active_bg': '#fffff0',
        'button_bg': '#c0c0c0',
        'button_fg': '#000000',
        'button_active_bg': '#a0a0a0',
        'button_active_fg': '#000000',
        'check_hover_bg': '#316ac5',
        'check_hover_fg': '#ffffff',
        'progress_bg': '#d4d0c8',
        'progress_fg': '#008080',
        'log_bg': '#000000',
        'log_fg': '#00ff00',
        'canvas_bg': '#ffffff',
        'curve_line': '#808080',
        'curve_peak': '#ff00ff',
        'curve_low': '#000080',
        'forget_curve': '#008080',
    },
}


def run_ffmpeg(args, label):
    cmd = ['ffmpeg', '-y', '-nostdin', *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or '').strip()
        preview = details[-2000:] if details else 'No ffmpeg output was returned.'
        lowered = preview.lower()
        if 'no space left on device' in lowered or 'not enough space' in lowered:
            raise RuntimeError(f'ffmpeg failed during {label}: disk space or temp-file write issue. {preview}')
        raise RuntimeError(f'ffmpeg failed during {label}: {preview}')


def build_preview_ffmpeg_args(input_path, output_path, duration=20):
    return ['-i', str(input_path), '-t', str(duration), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(output_path)]


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


def _probe_duration_seconds(path):
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return None
    result = subprocess.run(
        [ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
        capture_output=True, text=True,
    )
    value = result.stdout.strip()
    try:
        return float(value)
    except ValueError:
        return None


# ffmpeg splits the mpeg4/AVI intermediate into multiple RIFF segments once it
# passes roughly 1GB (see avimosh.parse_avi) — this tool only reads the first,
# so anything landing near or past that ceiling needs a smaller intermediate
# (higher --quality number) or a shorter clip.
INTERMEDIATE_SIZE_WARNING_BYTES = 800 * 1024 * 1024


def estimate_intermediate_size(input_path, quality, keyframe_interval=9999, sample_seconds=10,
                               presort_pixel_sort=None):
    """Transcodes a short sample with the exact settings run_pipeline's own
    transcode step uses, then extrapolates to the full clip's duration —
    a real estimate of the actual intermediate size, not a generic guess
    from resolution/duration alone. Samples from 10% into the clip rather
    than frame 0, since an intro/title card there tends to under-represent
    the rest of the video's complexity.

    presort_pixel_sort: pass {'direction', 'aggression', 'key'} when
    pixel-sort is set to run *before* the mosh — in that mode the real
    intermediate gets built from already-sorted footage, which compresses
    far worse than the clean source. Sampling the clean source alone
    badly underestimates the real size (this is exactly what let a real
    render pass this check and still fail hours later on the actual
    transcode)."""
    duration = _probe_duration_seconds(input_path)
    if not duration or duration <= sample_seconds:
        return None

    sample_start = min(duration * 0.1, duration - sample_seconds)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        transcode_source = input_path
        ss_args, t_args = ['-ss', str(sample_start)], ['-t', str(sample_seconds)]

        if presort_pixel_sort and pixelsort.PIXELSORT_AVAILABLE:
            sample_source = tmp_path / 'sample_source.mp4'
            run_ffmpeg([*ss_args, '-i', str(input_path), *t_args,
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-an', str(sample_source)],
                       'size-estimate-presort-extract')
            frames_dir = tmp_path / 'sample_frames'
            frame_paths = pixelsort.extract_frames(sample_source, frames_dir, label='size-estimate-presort-frames')
            pixelsort.sort_frames_in_place(
                frame_paths,
                direction=presort_pixel_sort.get('direction', 'both'),
                aggression=presort_pixel_sort.get('aggression', 0.5),
                key=presort_pixel_sort.get('key', 'brightness'),
            )
            sorted_sample = tmp_path / 'sample_sorted.mp4'
            pixelsort.reassemble_frames(frames_dir, _probe_fps(sample_source), sorted_sample,
                                        label='size-estimate-presort-reassemble')
            transcode_source = sorted_sample
            ss_args, t_args = [], []  # the sample is already just the short clip

        sample_path = tmp_path / 'sample.avi'
        run_ffmpeg([*ss_args, '-i', str(transcode_source), *t_args, '-c:v', 'mpeg4',
                    '-g', str(keyframe_interval), '-bf', '0', '-q:v', str(quality), '-an', str(sample_path)],
                   'size-estimate-sample')
        sample_bytes = sample_path.stat().st_size

    return (sample_bytes / sample_seconds) * duration


def apply_temporal_blend(input_path, output_path, mode='blend'):
    """Motion-aware smoothing via ffmpeg's minterpolate, run at the source's
    own frame rate (not up- or down-sampling fps) so it fills in
    duplicate/frozen stretches with motion-estimated content instead of
    just passing the same frames through untouched. scd=none matters: without
    it, minterpolate's scene-change detector treats the mosh's own frame
    jumps (keyframe removal, a freeze ending) as real cuts and skips
    interpolating exactly the frames we want smoothed.

    mode='blend': motion-compensated blending, gentler, cheaper.
    mode='motion': full motion-compensated interpolation (mi_mode=mci) —
    stronger smoothing, more likely to warp/ghost around the harshest jumps
    since it's more aggressively guessing at motion that isn't really there.
    """
    fps = _probe_fps(input_path)
    mi_mode = 'mci' if mode == 'motion' else 'blend'
    vf = f'minterpolate=fps={fps}:mi_mode={mi_mode}:scd=none'
    if mi_mode == 'mci':
        vf += ':mc_mode=aobmc:me_mode=bidir:vsbmc=1'
    if Path(output_path).suffix.lower() == '.webm':
        codec_args = ['-c:v', 'libvpx-vp9', '-crf', '30', '-b:v', '0']
    else:
        codec_args = ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18']
    run_ffmpeg(['-i', str(input_path), '-vf', vf, *codec_args, str(output_path)], 'temporal-blend')


def build_preview_config(config, preview_duration):
    preview_config = dict(config)
    preview_config['output'] = str(Path(config['input']).with_suffix('.preview.mp4'))
    preview_config['keep_intermediate'] = True
    preview_config['preview_duration'] = preview_duration
    return preview_config


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def resample_series(series, length):
    if not series:
        return [0.0] * length
    if len(series) == length:
        return [float(v) for v in series]
    out = []
    for i in range(length):
        src_idx = int(i * len(series) / length)
        out.append(float(series[min(src_idx, len(series) - 1)]))
    return out


def normalize_series(series):
    values = [float(v) for v in series]
    if not values:
        return []
    max_value = max(values)
    if max_value <= 0:
        return [0.0 for _ in values]
    return [clamp(v / max_value) for v in values]


def build_vividness_curve(length, motion_energy=None, audio_energy=None,
                          cycles=1.5, motion_weight=0.55, audio_weight=0.15,
                          t_offset=0.0, t_span=1.0, close_loop=True):
    """t_offset/t_span let a caller ask for just a slice of the overall wave
    (e.g. one chunk of a segmented long render) instead of always spanning
    the full 0-1 range — so the curve's phase continues smoothly across
    chunk boundaries rather than resetting at each one. close_loop should
    only be True for a curve covering the *whole* clip (it forces the first
    and last sample to match so an export can loop seamlessly) — pass False
    for an interior chunk, since matching a chunk's own start/end doesn't
    make the stitched whole loop-safe anyway."""
    if length <= 1:
        return [0.5]

    motion_energy = resample_series(motion_energy or [0.0] * length, length)
    audio_energy = resample_series(audio_energy or [0.0] * length, length)
    motion_norm = normalize_series(motion_energy)
    audio_norm = normalize_series(audio_energy)

    curve = []
    for i in range(length):
        local_t = i / max(length - 1, 1)
        t = t_offset + local_t * t_span
        base_wave = 0.5 + 0.5 * math.sin(2.0 * math.pi * cycles * t)
        blended = ((1.0 - motion_weight - audio_weight) * base_wave +
                   motion_weight * motion_norm[i] +
                   audio_weight * audio_norm[i])
        curve.append(clamp(blended))

    if close_loop:
        curve[0] = curve[-1] = (curve[0] + curve[-1]) / 2.0
    return curve


def forgetting_curve_retention(t, k=1.84, c=1.25):
    """Power-law retention model: b = 100k / ((log t)^c + k).
    t is 'time since the memory was formed', t >= 1 (so log t >= 0 and the
    fractional power stays real). Returns retention normalized to 0-1
    (1.0 at t=1, decaying toward 0 as t grows)."""
    t = max(1.0, t)
    denom = (math.log(t)) ** c + k
    return (100.0 * k / denom) / 100.0 if denom > 0 else 0.0


def build_forgetting_curve(length, cycles=1.5, k=1.84, c=1.25, t_max=60.0, t_offset=0.0, t_span=1.0):
    """Same cyclical rhythm as the vividness curve (same 'cycles' count),
    but each cycle is a fresh memory event: retention resets to 1.0 at the
    start of every cycle and decays per forgetting_curve_retention across
    it, rather than following a sine wave. t_offset/t_span: see
    build_vividness_curve — same idea, for continuing the cycle phase
    across segmented-render chunk boundaries."""
    if length <= 1:
        return [1.0]
    curve = []
    for i in range(length):
        local_t = i / max(length - 1, 1)
        t_norm = t_offset + local_t * t_span
        cycle_frac = (cycles * t_norm) % 1.0
        t_model = 1.0 + cycle_frac * (t_max - 1.0)
        curve.append(forgetting_curve_retention(t_model, k=k, c=c))
    return curve


def _read_pgm(path):
    with open(path, 'rb') as fh:
        magic = fh.readline().strip()
        if magic not in (b'P5', b'P2'):
            raise ValueError(f'unsupported PGM magic: {magic!r}')

        while True:
            line = fh.readline()
            if not line:
                raise ValueError(f'could not read PGM header from {path}')
            if line.startswith(b'#'):
                continue
            parts = line.split()
            if len(parts) >= 2:
                width, height = map(int, parts[:2])
                break

        while True:
            line = fh.readline()
            if not line:
                raise ValueError(f'could not read PGM max value from {path}')
            if line.startswith(b'#'):
                continue
            max_value = int(line)
            break

        pixels = fh.read()
        if len(pixels) < width * height:
            raise ValueError(f'not enough pixel data in {path}')
        return [pixel / max_value for pixel in pixels[:width * height]]


def analyze_motion_energy(input_path, target_length, analysis_fps=4.0, size=64):
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    analysis_dir = Path(tempfile.mkdtemp(prefix='memory-mosh-analysis-', dir=str(TEMP_ROOT)))
    try:
        frame_count = max(8, min(96, target_length))
        run_ffmpeg([
            '-i', str(input_path),
            '-vf', f'fps={analysis_fps},scale={size}:{size}:flags=lanczos,format=gray',
            '-frames:v', str(frame_count),
            '-f', 'image2', str(analysis_dir / 'frame_%04d.pgm')
        ], 'analyze-motion')

        frame_paths = sorted(analysis_dir.glob('frame_*.pgm'))
        if not frame_paths:
            return [0.0] * target_length

        frames = [_read_pgm(path) for path in frame_paths]
        if len(frames) <= 1:
            return [0.0] * target_length

        motion = []
        for idx, frame in enumerate(frames):
            if idx == 0:
                motion.append(0.0)
                continue
            prev = frames[idx - 1]
            diff = sum(abs(cur - prev_val) for cur, prev_val in zip(frame, prev)) / max(len(frame), 1)
            motion.append(diff)

        return resample_series(motion, target_length)
    finally:
        shutil.rmtree(analysis_dir, ignore_errors=True)


def analyze_audio_energy(input_path, target_length, sample_rate=8000, window=256):
    try:
        proc = subprocess.run(
            ['ffmpeg', '-y', '-i', str(input_path), '-vn', '-ac', '1', '-ar', str(sample_rate), '-f', 's16le', '-'],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            return [0.0] * target_length

        data = proc.stdout
        if not data:
            return [0.0] * target_length

        samples = [int.from_bytes(data[i:i + 2], 'little', signed=True) for i in range(0, len(data) - 1, 2)]
        if not samples:
            return [0.0] * target_length

        energy = []
        for idx in range(0, len(samples), window):
            chunk = samples[idx:idx + window]
            if not chunk:
                continue
            rms = sum(value * value for value in chunk) / len(chunk)
            energy.append(math.sqrt(rms) / 32768.0)

        return resample_series(energy, target_length)
    except Exception:
        return [0.0] * target_length


def analyze_vividness_curve(input_path, target_length, analysis_fps=4.0,
                            cycles=1.5, motion_weight=0.55, audio_weight=0.15,
                            include_audio=False, t_offset=0.0, t_span=1.0, close_loop=True):
    motion_energy = analyze_motion_energy(input_path, target_length, analysis_fps=analysis_fps)
    audio_energy = []
    if include_audio:
        audio_energy = analyze_audio_energy(input_path, target_length)
    return build_vividness_curve(target_length, motion_energy=motion_energy,
                                 audio_energy=audio_energy,
                                 cycles=cycles,
                                 motion_weight=motion_weight,
                                 audio_weight=audio_weight,
                                 t_offset=t_offset, t_span=t_span, close_loop=close_loop)


def run_pipeline(config, progress_callback=None):
    if shutil.which('ffmpeg') is None:
        raise RuntimeError('ffmpeg not found on PATH. Install it from https://ffmpeg.org/download.html')

    in_path = Path(config['input'])
    out_path = Path(config['output'])
    if not in_path.exists():
        raise FileNotFoundError(f'input file not found: {in_path}')

    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix='memory-mosh-', dir=str(TEMP_ROOT))) if not config.get('keep_intermediate', False) else out_path.parent
    raw_avi = workdir / f'{in_path.stem}_raw.avi'
    moshed_avi = workdir / f'{in_path.stem}_moshed.avi'

    # Stage checkpoints for the overall progress bar. Subject-protect and
    # pixel-sort (when it runs after the mosh) are merged into one
    # 'frame_effects' stage — extract frames once, apply whichever of the
    # two are enabled in place on the same files, reassemble once — rather
    # than each doing its own full encode/decode round trip. Temporal blend
    # (motion interpolation) can't join that: it's a single ffmpeg filter
    # pass, not a per-frame Python step.
    STAGE_WEIGHTS = {'frame_effects': 0.85, 'blend': 0.15}
    subject_protect_enabled = config.get('subject_protect_enabled', False)
    pixel_sort_enabled = config.get('pixel_sort_enabled', False)
    temporal_blend_enabled = config.get('temporal_blend_enabled', False)
    # Pixel-sort can run before the mosh (sorted footage then gets moshed —
    # a fused, painterly result since the lossy mosh transcode mangles the
    # sort streaks further) or after it (the default — mosh corruption and
    # sort streaks stay legible as separate layers). Subject-protect always
    # needs the moshed output to composite against, so it can't move.
    pixel_sort_before_mosh = pixel_sort_enabled and config.get('pixel_sort_timing', 'after-mosh') == 'before-mosh'
    frame_effects_pixel_sort = pixel_sort_enabled and not pixel_sort_before_mosh
    frame_effects_enabled = subject_protect_enabled or frame_effects_pixel_sort
    extra_stages = []
    if frame_effects_enabled:
        extra_stages.append('frame_effects')
    if temporal_blend_enabled:
        extra_stages.append('blend')
    reencode_end = 0.55 if extra_stages else 0.95
    total_extra_weight = sum(STAGE_WEIGHTS[s] for s in extra_stages) or 1.0

    # For a segmented long-form render, each chunk is a slice of the whole
    # timeline — these keep the vividness/forgetting curves' phase
    # continuous across chunk boundaries instead of resetting per chunk.
    # Defaults (0, 1, True) reproduce the old single-pass behavior exactly.
    curve_t_offset = config.get('curve_phase_offset', 0.0)
    curve_t_span = config.get('curve_phase_span', 1.0)
    curve_close_loop = config.get('curve_close_loop', True)

    source_path = in_path
    if config.get('preview_duration'):
        preview_path = workdir / f'{in_path.stem}_preview.mp4'
        if progress_callback:
            progress_callback(f'Creating a {config["preview_duration"]}s preview clip…', 0.02)
        run_ffmpeg(['-i', str(in_path), '-t', str(config['preview_duration']), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-an', str(preview_path)], 'preview-trim')
        source_path = preview_path

    # The true, unaltered clean source — kept separate from source_path
    # (which pixel-sort-before-mosh reassigns below) so subject-protect
    # always composites against genuinely clean footage, not pre-sorted.
    true_clean_source = source_path

    # Pixel-sort-before-mosh is by far the slowest single step when active,
    # so it claims a real chunk of the progress bar up front (40%) instead
    # of being squeezed into the same sliver the quick preview-trim gets.
    # Everything after it gets proportionally rescaled into what's left.
    remaining_start = 0.0
    if pixel_sort_before_mosh:
        remaining_start = 0.40
        if progress_callback:
            progress_callback('Pixel sorting the clean source before moshing…', 0.0)
        presort_dir = workdir / 'presort_frames'
        presort_frame_paths = pixelsort.extract_frames(source_path, presort_dir, label='presort-extract')

        presort_curve = None
        if config.get('use_vividness_curve', False):
            presort_curve = build_forgetting_curve(len(presort_frame_paths), cycles=config.get('curve_cycles', 1.5),
                                                    t_offset=curve_t_offset, t_span=curve_t_span)

        def _presort_progress(message, fraction=None):
            if progress_callback:
                progress_callback(message, remaining_start * (fraction if fraction is not None else 0.0))

        pixelsort.sort_frames_in_place(
            presort_frame_paths,
            direction=config.get('pixel_sort_direction', 'both'),
            aggression=config.get('pixel_sort_aggression', 0.5),
            key=config.get('pixel_sort_key', 'brightness'),
            decay_curve=presort_curve,
            progress_callback=_presort_progress, progress_start=0.05, progress_end=0.9,
        )

        presorted_path = workdir / f'{in_path.stem}_presorted.mp4'
        pixelsort.reassemble_frames(presort_dir, _probe_fps(source_path), presorted_path, label='presort-reassemble')
        source_path = presorted_path

    if remaining_start:
        _inner_progress_callback = progress_callback

        def _remapped_progress_callback(message, fraction=None):
            if _inner_progress_callback:
                remapped = remaining_start + (1.0 - remaining_start) * (fraction if fraction is not None else 0.0)
                _inner_progress_callback(message, remapped)

        progress_callback = _remapped_progress_callback

    if progress_callback:
        progress_callback('Transcoding to a raw AVI/mpeg4 intermediate…', 0.05)
    run_ffmpeg(['-i', str(source_path), '-c:v', 'mpeg4', '-g', str(config.get('keyframe_interval', 9999)), '-bf', '0',
                '-q:v', str(config['quality']), '-an', str(raw_avi)], 'transcode')

    raw_data = raw_avi.read_bytes()
    _, _, movi, _ = avimosh.parse_avi(raw_data)
    frame_count = len(avimosh.parse_movi_frames(raw_data, movi))

    vividness_curve = None
    if config.get('use_vividness_curve', False):
        if progress_callback:
            progress_callback('Analyzing motion into a vividness curve…', 0.15)
        vividness_curve = analyze_vividness_curve(
            in_path,
            target_length=frame_count,
            analysis_fps=config.get('analysis_fps', 4.0),
            cycles=config.get('curve_cycles', 1.5),
            motion_weight=config.get('motion_weight', 0.55),
            audio_weight=config.get('audio_weight', 0.15),
            include_audio=config.get('include_audio', False),
            t_offset=curve_t_offset, t_span=curve_t_span, close_loop=curve_close_loop,
        )

    if progress_callback:
        progress_callback('Removing keyframes and duplicating delta frames…', 0.25)
    stats = avimosh.mosh_file(
        str(raw_avi), str(moshed_avi),
        keyframe_removal_rate=config['keyframe_removal_rate'],
        duplicate_rate=config['duplicate_rate'],
        duplicate_range=(config['duplicate_min'], config['duplicate_max']),
        freeze_chance=config['freeze_chance'],
        freeze_range=(config['freeze_min'], config['freeze_max']),
        seed=config['seed'],
        vividness_curve=vividness_curve,
    )

    if progress_callback:
        progress_callback('Re-encoding to a shareable output file…', reencode_end * 0.8)
    if out_path.suffix.lower() == '.webm':
        codec_args = ['-c:v', 'libvpx-vp9', '-crf', '30', '-b:v', '0']
    else:
        codec_args = ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18']
    run_ffmpeg(['-i', str(moshed_avi), *codec_args, str(out_path)], 're-encode')

    stage_cursor = reencode_end

    def _next_stage_span(name):
        nonlocal stage_cursor
        start = stage_cursor
        end = start + (0.98 - reencode_end) * (STAGE_WEIGHTS[name] / total_extra_weight)
        stage_cursor = end
        return start, end

    if frame_effects_enabled:
        stage_start, stage_end = _next_stage_span('frame_effects')

        def _frame_progress(message, fraction=None, _start=stage_start, _end=stage_end):
            if progress_callback:
                overall = _start + (_end - _start) * (fraction if fraction is not None else 0.0)
                progress_callback(message, overall)

        frames_input = workdir / f'{in_path.stem}_pre_frame_effects{out_path.suffix}'
        shutil.move(str(out_path), str(frames_input))

        fps = _probe_fps(frames_input)
        frames_dir = workdir / 'frame_effects_frames'
        _frame_progress('Extracting frames for subject-protect/pixel-sort…', 0.0)
        frame_paths = pixelsort.extract_frames(frames_input, frames_dir, label='frame-effects-extract')

        # Sub-spans within this stage's local [0, 1] fraction — extraction
        # and reassembly get small fixed slices, the rest is split between
        # whichever of protect/pixel-sort are actually enabled.
        cursor = 0.05

        if subject_protect_enabled:
            clean_frames_dir = workdir / 'frame_effects_clean'
            _frame_progress('Subject protect: extracting clean source frames…', cursor)
            clean_frame_paths = pixelsort.extract_frames(
                true_clean_source, clean_frames_dir, label='frame-effects-extract-clean')

            protect_end = cursor + (0.45 if frame_effects_pixel_sort else 0.85)
            subject_protect.composite_frames_in_place(
                frame_paths, clean_frame_paths,
                channel=config.get('subject_protect_channel', 'hue'),
                low=config.get('subject_protect_low', 0.0),
                high=config.get('subject_protect_high', 60.0),
                progress_callback=_frame_progress, progress_start=cursor, progress_end=protect_end,
            )
            cursor = protect_end

        if frame_effects_pixel_sort:
            forgetting_curve = None
            if config.get('use_vividness_curve', False):
                forgetting_curve = build_forgetting_curve(frame_count, cycles=config.get('curve_cycles', 1.5),
                                                           t_offset=curve_t_offset, t_span=curve_t_span)

            sort_end = 0.9
            pixelsort.sort_frames_in_place(
                frame_paths,
                direction=config.get('pixel_sort_direction', 'both'),
                aggression=config.get('pixel_sort_aggression', 0.5),
                key=config.get('pixel_sort_key', 'brightness'),
                decay_curve=forgetting_curve,
                progress_callback=_frame_progress, progress_start=cursor, progress_end=sort_end,
            )
            cursor = sort_end

        _frame_progress('Re-assembling video…', 0.95)
        pixelsort.reassemble_frames(frames_dir, fps, out_path, label='frame-effects-reassemble')

    if temporal_blend_enabled:
        stage_start, stage_end = _next_stage_span('blend')

        if progress_callback:
            progress_callback('Smoothing with a temporal blend…', stage_start)
        blend_input = workdir / f'{in_path.stem}_pre_blend{out_path.suffix}'
        shutil.move(str(out_path), str(blend_input))
        apply_temporal_blend(blend_input, out_path, mode=config.get('temporal_blend_mode', 'blend'))
        if progress_callback:
            progress_callback('Temporal blend complete.', stage_end)

    if not config.get('keep_intermediate', False):
        shutil.rmtree(workdir, ignore_errors=True)

    stats['output_path'] = str(out_path)
    return stats


# ffmpeg splits the mpeg4/AVI intermediate into multiple RIFF segments once
# it passes roughly 1GB (see avimosh.parse_avi's safety check) — this
# targets comfortably under that so a chunk's own intermediate shouldn't
# get there itself.
CHUNK_TARGET_BYTES = 700 * 1024 * 1024


def presort_pixel_sort_config(config):
    """Build the presort_pixel_sort dict estimate_intermediate_size needs
    when pixel-sort is set to run before the mosh — None otherwise (i.e.
    the common case: sample the clean source directly, no detour)."""
    if not config.get('pixel_sort_enabled') or config.get('pixel_sort_timing', 'after-mosh') != 'before-mosh':
        return None
    return {
        'direction': config.get('pixel_sort_direction', 'both'),
        'aggression': config.get('pixel_sort_aggression', 0.5),
        'key': config.get('pixel_sort_key', 'brightness'),
    }


def estimate_segment_plan(input_path, quality, keyframe_interval=9999, chunk_target_bytes=CHUNK_TARGET_BYTES,
                          presort_pixel_sort=None):
    """Returns (estimated_total_bytes, duration_seconds, chunk_count), or
    None if duration/size can't be determined. chunk_count is 1 whenever a
    single pass would stay safely under the size ceiling."""
    estimated = estimate_intermediate_size(input_path, quality, keyframe_interval,
                                           presort_pixel_sort=presort_pixel_sort)
    duration = _probe_duration_seconds(input_path)
    if not estimated or not duration:
        return None
    if estimated < INTERMEDIATE_SIZE_WARNING_BYTES:
        return estimated, duration, 1
    bytes_per_second = estimated / duration
    chunk_seconds = max(30.0, chunk_target_bytes / bytes_per_second)
    chunk_count = max(1, math.ceil(duration / chunk_seconds))
    return estimated, duration, chunk_count


def run_pipeline_auto(config, progress_callback=None):
    """The entry point the GUI and CLI actually call. Behaves exactly like
    run_pipeline for anything that fits in a single pass. For a source
    whose mpeg4/AVI intermediate would land at or past avimosh's single-
    RIFF-segment ceiling, transparently splits the source into chunks, runs
    each through the unmodified run_pipeline, and stitches the results back
    into one output — instead of failing partway through, or (pre-safety-
    check) silently dropping most of the video."""
    if config.get('preview_duration'):
        # Previews are always short — never worth the size-estimate pass.
        return run_pipeline(config, progress_callback=progress_callback)

    plan = estimate_segment_plan(config['input'], config.get('quality', 3), config.get('keyframe_interval', 9999),
                                 presort_pixel_sort=presort_pixel_sort_config(config))
    if not plan or plan[2] <= 1:
        return run_pipeline(config, progress_callback=progress_callback)

    _estimated, duration, chunk_count = plan
    return _run_segmented(config, duration, chunk_count, progress_callback)


def _run_segmented(config, duration, chunk_count, progress_callback=None):
    in_path = Path(config['input'])
    out_path = Path(config['output'])
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix='memory-mosh-segments-', dir=str(TEMP_ROOT)))

    try:
        chunk_seconds = duration / chunk_count
        if progress_callback:
            progress_callback(
                f'Source is too large for a single pass — splitting into {chunk_count} segments…', 0.0)

        segments_dir = workdir / 'source_segments'
        segments_dir.mkdir(parents=True)
        # Stream-copy split (no re-encode) — can only cut at source
        # keyframes, so actual chunk lengths vary a bit from chunk_seconds;
        # that's fine, we only need each one comfortably under the ceiling.
        run_ffmpeg([
            '-i', str(in_path), '-map', '0:v:0', '-c', 'copy',
            '-f', 'segment', '-segment_time', str(chunk_seconds), '-reset_timestamps', '1',
            str(segments_dir / 'chunk_%04d.mp4'),
        ], 'segment-split')

        chunk_paths = sorted(segments_dir.glob('chunk_*.mp4'))
        n = len(chunk_paths)
        if n == 0:
            raise RuntimeError('Splitting the source into segments produced no output — check the source file.')

        base_seed = config.get('seed')
        chunk_outputs = []
        totals = {'keyframes_removed': 0, 'frames_duplicated': 0, 'original_frames': 0, 'output_frames': 0}

        for idx, chunk_path in enumerate(chunk_paths):
            def _chunk_progress(message, fraction=None, _idx=idx, _n=n):
                if progress_callback:
                    overall = ((_idx + (fraction if fraction is not None else 0.0)) / _n) * 0.95
                    progress_callback(f'[segment {_idx + 1}/{_n}] {message}', overall)

            chunk_config = dict(config)
            chunk_config['input'] = str(chunk_path)
            chunk_output = workdir / f'chunk_out_{idx:04d}{out_path.suffix}'
            chunk_config['output'] = str(chunk_output)
            chunk_config['keep_intermediate'] = False
            # Keeps the vividness/forgetting curves' phase continuous across
            # chunk boundaries — this chunk covers [idx/n, (idx+1)/n) of the
            # whole timeline, not its own fresh 0-1 span.
            chunk_config['curve_phase_offset'] = idx / n
            chunk_config['curve_phase_span'] = 1.0 / n
            chunk_config['curve_close_loop'] = False
            if base_seed is not None:
                chunk_config['seed'] = base_seed + idx  # distinct but reproducible per chunk

            chunk_result = run_pipeline(chunk_config, progress_callback=_chunk_progress)
            chunk_outputs.append(chunk_output)
            for key in totals:
                totals[key] += chunk_result.get(key, 0)

        if progress_callback:
            progress_callback('Stitching segments back together…', 0.97)

        concat_list = workdir / 'concat_list.txt'
        with open(concat_list, 'w', encoding='utf-8') as fh:
            for chunk_output in chunk_outputs:
                escaped = str(chunk_output.resolve()).replace("'", "'\\''")
                fh.write(f"file '{escaped}'\n")
        run_ffmpeg(['-f', 'concat', '-safe', '0', '-i', str(concat_list), '-c', 'copy', str(out_path)],
                   'segment-concat')

        if progress_callback:
            progress_callback('Done.', 1.0)

        return {
            'output_path': str(out_path),
            'segmented': True,
            'segment_count': n,
            **totals,
        }
    finally:
        if not config.get('keep_intermediate', False):
            shutil.rmtree(workdir, ignore_errors=True)


# Defaults for every GUI setting that gets persisted (last-used state,
# presets, and the "Reset to Default" button all key off this). Doesn't
# include input/output paths or theme-independent transient state
# (status text, progress, size warnings) — those aren't "settings" in
# the recipe sense, they're per-session/per-file.
SETTINGS_DEFAULTS = {
    'use_curve': True, 'audio': False, 'analysis_fps': '4.0', 'cycles': '1.5',
    'motion_weight': '0.55', 'audio_weight': '0.15', 'force_keyframe_interval': False,
    'keyframe_interval': '60', 'keyframe_rate': '0.9', 'duplicate_rate': '0.15',
    'duplicate_repeat_min': '2', 'duplicate_repeat_max': '4', 'freeze_chance': '0.02',
    'freeze_min': '6', 'freeze_max': '18', 'quality': '3', 'seed': '', 'preview': '15',
    'pixel_sort': False, 'pixel_sort_direction': 'both', 'pixel_sort_aggression': '0.5',
    'pixel_sort_key': 'brightness', 'pixel_sort_timing': 'after-mosh',
    'temporal_blend': False, 'temporal_blend_mode': 'blend',
    'subject_protect': False, 'subject_protect_channel': 'hue',
    'subject_protect_low': '0', 'subject_protect_high': '60',
    'theme': 'ember',
}
PRESET_SLOTS = ('1', '2', '3')


class MemoryMoshApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Memory Mosh')
        self.geometry('1080x860')
        self.minsize(820, 760)
        self.resizable(True, True)
        self._apply_styles()

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.input_duration_var = tk.StringVar(value='')
        self.size_warning_var = tk.StringVar(value='')
        self.use_curve_var = tk.BooleanVar(value=True)
        self.audio_var = tk.BooleanVar(value=False)
        self.analysis_fps_var = tk.StringVar(value='4.0')
        self.cycles_var = tk.StringVar(value='1.5')
        self.motion_weight_var = tk.StringVar(value='0.55')
        self.audio_weight_var = tk.StringVar(value='0.15')
        self.force_keyframe_interval_var = tk.BooleanVar(value=False)
        self.keyframe_interval_var = tk.StringVar(value='60')
        self.keyframe_rate_var = tk.StringVar(value='0.9')
        self.duplicate_rate_var = tk.StringVar(value='0.15')
        self.duplicate_repeat_min_var = tk.StringVar(value='2')
        self.duplicate_repeat_max_var = tk.StringVar(value='4')
        self.freeze_chance_var = tk.StringVar(value='0.02')
        self.freeze_min_var = tk.StringVar(value='6')
        self.freeze_max_var = tk.StringVar(value='18')
        self.quality_var = tk.StringVar(value='3')
        self.seed_var = tk.StringVar(value='')
        self.preview_var = tk.StringVar(value='15')
        self.pixel_sort_var = tk.BooleanVar(value=False)
        self.pixel_sort_direction_var = tk.StringVar(value='both')
        self.pixel_sort_aggression_var = tk.StringVar(value='0.5')
        self.pixel_sort_key_var = tk.StringVar(value='brightness')
        self.pixel_sort_timing_var = tk.StringVar(value='after-mosh')
        self.temporal_blend_var = tk.BooleanVar(value=False)
        self.temporal_blend_mode_var = tk.StringVar(value='blend')
        self.subject_protect_var = tk.BooleanVar(value=False)
        self.subject_protect_channel_var = tk.StringVar(value='hue')
        self.subject_protect_low_var = tk.StringVar(value='0')
        self.subject_protect_high_var = tk.StringVar(value='60')
        self.theme_var = tk.StringVar(value='ember')
        self.preset_slot_var = tk.StringVar(value='1')
        self._description_labels = []
        self._resize_after_id = None

        self._user_data = self._load_user_data()
        self._apply_settings(self._user_data.get('last_used', {}))

        self._build_ui()
        self.protocol('WM_DELETE_WINDOW', self._on_close)

    def _load_user_data(self):
        """Reads user_settings.json (last-used state + presets). Never
        raises — a missing or corrupt file just means starting fresh
        with defaults, same as first run."""
        if not SETTINGS_PATH.exists():
            return {}
        try:
            with open(SETTINGS_PATH, 'r', encoding='utf-8') as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_user_data(self):
        """Writes via a temp file + os.replace so a crash/forced reboot
        mid-write can't leave user_settings.json half-written and
        unreadable next launch (the exact failure mode that made a
        render's own output file unreadable after last night's forced
        Windows Update restart)."""
        try:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = SETTINGS_PATH.with_suffix('.json.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as fh:
                json.dump(self._user_data, fh, indent=2)
            os.replace(tmp_path, SETTINGS_PATH)
        except OSError:
            pass  # settings persistence is a convenience, never worth crashing the app over

    def _collect_settings(self):
        return {name: getattr(self, f'{name}_var').get() for name in SETTINGS_DEFAULTS}

    def _apply_settings(self, data):
        for name, value in data.items():
            if name not in SETTINGS_DEFAULTS:
                continue  # ignore unknown/stale keys rather than erroring
            try:
                getattr(self, f'{name}_var').set(value)
            except Exception:
                pass  # a malformed single value shouldn't block the rest from loading

    def _save_last_used(self):
        self._user_data['last_used'] = self._collect_settings()
        self._save_user_data()

    def _reset_to_defaults(self):
        self._apply_settings(SETTINGS_DEFAULTS)
        self._apply_styles()
        self._append_log('Settings reset to defaults.')

    def _save_preset(self):
        slot = self.preset_slot_var.get()
        self._user_data.setdefault('presets', {})[slot] = self._collect_settings()
        self._save_user_data()
        self._append_log(f'Saved current settings to preset {slot}.')

    def _load_preset(self):
        slot = self.preset_slot_var.get()
        data = self._user_data.get('presets', {}).get(slot)
        if not data:
            messagebox.showinfo('Empty preset', f'Preset {slot} is empty — save something to it first.')
            return
        self._apply_settings(data)
        self._apply_styles()
        self._append_log(f'Loaded preset {slot}.')

    def _on_close(self):
        self._save_last_used()
        self.destroy()

    def _build_ui(self):
        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        scrollbar = Scrollbar(self, orient='vertical', command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        scroll_frame.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=scroll_frame, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

        def _on_mouse_wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')

        def _on_touchpad(event):
            canvas.yview_scroll(int(-1 * event.delta), 'units')

        canvas.bind_all('<MouseWheel>', _on_mouse_wheel)
        canvas.bind_all('<Shift-MouseWheel>', _on_mouse_wheel)
        canvas.bind_all('<Button-4>', _on_touchpad)
        canvas.bind_all('<Button-5>', _on_touchpad)

        frame = ttk.Frame(scroll_frame, padding=16)
        frame.pack(fill='both', expand=True)
        frame.configure(style='TFrame')

        frame.columnconfigure(0, minsize=120)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, minsize=92)

        ttk.Label(frame, text='Memory Mosh', style='Title.TLabel').grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 10))

        ttk.Label(frame, text='Theme', style='Control.TLabel').grid(row=1, column=0, sticky='w', pady=(0, 6))
        theme_combo = ttk.Combobox(frame, textvariable=self.theme_var, values=list(THEMES.keys()), state='readonly', width=14)
        theme_combo.grid(row=1, column=1, sticky='w', padx=6, pady=(0, 6))
        theme_combo.bind('<<ComboboxSelected>>', lambda _event: self._apply_styles())

        ttk.Label(frame, text='Input video', style='Control.TLabel').grid(row=2, column=0, sticky='w', pady=(0, 4))
        ttk.Entry(frame, textvariable=self.input_var, width=44, style='Entry.TEntry').grid(row=2, column=1, sticky='ew', padx=6, pady=(0, 4))
        ttk.Button(frame, text='Browse', command=self._pick_input, style='Secondary.TButton').grid(row=2, column=2, padx=4, pady=(0, 4), sticky='ew')
        input_info = ttk.Frame(frame)
        input_info.grid(row=3, column=1, columnspan=2, sticky='w', padx=6, pady=(0, 4))
        ttk.Label(input_info, textvariable=self.input_duration_var, style='Description.TLabel').pack(anchor='w')
        size_warning_label = ttk.Label(input_info, textvariable=self.size_warning_var, foreground='#e0793c', wraplength=460, justify='left')
        size_warning_label.pack(anchor='w')
        self._description_labels.append(size_warning_label)

        ttk.Label(frame, text='Output video', style='Control.TLabel').grid(row=4, column=0, sticky='w', pady=(0, 4))
        ttk.Entry(frame, textvariable=self.output_var, width=44, style='Entry.TEntry').grid(row=4, column=1, sticky='ew', padx=6, pady=(0, 4))
        ttk.Button(frame, text='Browse', command=self._pick_output, style='Secondary.TButton').grid(row=4, column=2, padx=4, pady=(0, 4), sticky='ew')

        checkbox_row = ttk.Frame(frame)
        checkbox_row.grid(row=5, column=1, sticky='w', pady=(6, 2))
        checkbox_row.columnconfigure(0, weight=1)
        checkbox_row.columnconfigure(1, weight=1)
        ttk.Checkbutton(checkbox_row, text='Use vividness curve', variable=self.use_curve_var).grid(row=0, column=0, sticky='w', padx=(0, 16))
        ttk.Checkbutton(checkbox_row, text='Analyze audio energy', variable=self.audio_var).grid(row=0, column=1, sticky='w')
        ttk.Checkbutton(checkbox_row, text='Pixel sort (post-process)', variable=self.pixel_sort_var).grid(row=1, column=0, sticky='w', padx=(0, 16), pady=(4, 0))
        ttk.Checkbutton(checkbox_row, text='Smooth (motion interpolation)', variable=self.temporal_blend_var).grid(row=1, column=1, sticky='w', pady=(4, 0))
        ttk.Checkbutton(checkbox_row, text='Subject protect (HSL mask)', variable=self.subject_protect_var).grid(row=2, column=0, sticky='w', padx=(0, 16), pady=(4, 0))
        ttk.Checkbutton(checkbox_row, text='Force periodic keyframes', variable=self.force_keyframe_interval_var).grid(row=2, column=1, sticky='w', pady=(4, 0))

        effect_groups = [
            ('Curve', [
                {'label': 'Analysis fps', 'variable': self.analysis_fps_var, 'from_': 1.0, 'to': 10.0, 'step': 0.5, 'description': 'How densely the source is sampled for the vividness curve.'},
                {'label': 'Curve cycles', 'variable': self.cycles_var, 'from_': 0.5, 'to': 6.0, 'step': 0.1, 'description': 'How many wave cycles sweep across the clip.'},
                {'label': 'Motion weight', 'variable': self.motion_weight_var, 'from_': 0.0, 'to': 1.0, 'step': 0.05, 'description': 'How strongly motion influences the intensity curve.'},
                {'label': 'Audio weight', 'variable': self.audio_weight_var, 'from_': 0.0, 'to': 1.0, 'step': 0.05, 'description': 'How strongly audio energy nudges the curve.'},
            ], 7),
            ('Mosh', [
                {'label': 'Keyframe interval', 'variable': self.keyframe_interval_var, 'from_': 2, 'to': 300, 'step': 1,
                 'description': 'Needs "Force periodic keyframes" checked, else ignored (old behavior: one keyframe '
                                'total). Gives keyframe removal real material on a static shot with no scene cuts — '
                                'a removed keyframe drags a stale reference into new motion vectors, relocating '
                                'content into the wrong part of the frame instead of just freezing. Try 15-60.'},
                {'label': 'Keyframe removal rate', 'variable': self.keyframe_rate_var, 'from_': 0.0, 'to': 1.0, 'step': 0.05, 'description': 'How often real keyframes are dropped.'},
                {'label': 'Duplicate rate', 'variable': self.duplicate_rate_var, 'from_': 0.0, 'to': 1.0, 'step': 0.05, 'description': 'How often delta frames are repeated for glitch streaks.'},
                {'label': 'Duplicate repeat min', 'variable': self.duplicate_repeat_min_var, 'from_': 1, 'to': 12, 'step': 1,
                 'description': 'How many times a triggered duplicate repeats (randomized min-max) — the actual '
                                'motion-drag effect. At 1, it\'s a no-op: zero drag. Higher also makes the output '
                                'video longer (each repeat adds a frame).'},
                {'label': 'Duplicate repeat max', 'variable': self.duplicate_repeat_max_var, 'from_': 1, 'to': 12, 'step': 1,
                 'description': 'Upper end of the range above. Keep >= min.'},
                {'label': 'Freeze chance', 'variable': self.freeze_chance_var, 'from_': 0.0, 'to': 0.25, 'step': 0.01, 'description': 'How often the corruption holds longer as a freeze.'},
                {'label': 'Freeze min', 'variable': self.freeze_min_var, 'from_': 1, 'to': 24, 'step': 1, 'description': 'Shortest freeze length in frames.'},
                {'label': 'Freeze max', 'variable': self.freeze_max_var, 'from_': 2, 'to': 48, 'step': 1, 'description': 'Longest freeze length in frames.'},
            ], 11),
            ('Output', [
                {'label': 'Quality', 'variable': self.quality_var, 'from_': 1, 'to': 31, 'step': 1, 'description': 'The intermediate AVI quality. Lower is cleaner.'},
                {'label': 'Preview duration (s)', 'variable': self.preview_var, 'from_': 5, 'to': 30, 'step': 1, 'description': 'How long the preview export should be.'},
                {'label': 'Seed', 'variable': self.seed_var, 'kind': 'entry', 'description': 'Optional random seed for repeatable glitches.'},
            ], 15),
            ('Pixel Sort', [
                {'label': 'Direction', 'variable': self.pixel_sort_direction_var, 'kind': 'combo',
                 'values': ['rows', 'cols', 'both'],
                 'description': 'Which axis pixel runs get sorted along. "both" compounds rows then columns.'},
                {'label': 'Sort key', 'variable': self.pixel_sort_key_var, 'kind': 'combo',
                 'values': ['brightness', 'hue', 'saturation', 'lightness'],
                 'description': 'What decides which pixels get sorted. Brightness: light/dark streaks. Hue: color-'
                                'angle shifts, not light/dark. Saturation: vivid vs. washed-out. Lightness: '
                                'color-neutral brightness, subtler than Brightness.'},
                {'label': 'Aggression', 'variable': self.pixel_sort_aggression_var, 'from_': 0.0, 'to': 1.0, 'step': 0.05,
                 'description': 'How wide the sort-key window is. Higher = more of the frame dissolves. With "Use '
                                'vividness curve" on, this is a ceiling: forgotten (low-vividness) moments dissolve '
                                'toward it, vivid moments stay clean.'},
                {'label': 'Timing', 'variable': self.pixel_sort_timing_var, 'kind': 'combo',
                 'values': ['after-mosh', 'before-mosh'],
                 'description': 'After mosh (default): sort and mosh corruption stay legible as separate layers. '
                                'Before mosh: the sorted footage gets moshed too, fusing both into one rougher, '
                                'painterly texture. Slower — sorting runs on the full clean clip either way, but '
                                'before-mosh also means the mosh/re-encode steps work on already-sorted footage.'},
            ], 19),
            ('Smoothing', [
                {'label': 'Smooth mode', 'variable': self.temporal_blend_mode_var, 'kind': 'combo',
                 'values': ['blend', 'motion'],
                 'description': 'Runs last. Motion-aware smoothing (ffmpeg minterpolate) — fills frozen/duplicate '
                                'stretches instead of just blurring around them. Blend: gentler. Motion: stronger, '
                                'more prone to warping around hard jumps (keyframe removal, freeze endings).'},
            ], 22),
            ('Subject Protect', [
                {'label': 'Channel', 'variable': self.subject_protect_channel_var, 'kind': 'combo',
                 'values': ['hue', 'saturation', 'lightness', 'brightness'],
                 'description': 'Runs after the mosh, before pixel-sort/smoothing. Pixels in the Low-High range '
                                '(measured on the clean frame) play normally; everything else glitches as usual. '
                                'Lightness/brightness = tone target, hue = color family, saturation = vivid-vs-'
                                'washed-out. Reference ranges under Protect low/high.'},
                {'label': 'Protect low', 'variable': self.subject_protect_low_var, 'from_': 0, 'to': 255, 'step': 1,
                 'description': 'Low end of the range (0-255). Tone (lightness/brightness): black ~0-50, gray '
                                '~100-160, white ~200-255. Saturation: washed-out ~0-40, vivid ~150-255.'},
                {'label': 'Protect high', 'variable': self.subject_protect_high_var, 'from_': 0, 'to': 255, 'step': 1,
                 'description': 'High end of the range. Hue bands (0-255 = 0-360°): red ~0-15 & ~240-255 (wraps — '
                                'no single range covers pure red), orange ~15-30, yellow ~30-50, green ~60-110, '
                                'cyan ~115-140, blue ~145-185, purple ~190-230. Skin tones ~0-35.'},
            ], 25),
        ]

        self.group_frames = {}
        for group_name, items, start_row in effect_groups:
            group = ttk.LabelFrame(frame, text=group_name, padding=8)
            group.grid(row=start_row, column=0, columnspan=3, sticky='nsew', pady=6)
            group.configure(style='Section.TLabelframe')
            group.configure(padding=10)
            self.group_frames[group_name] = group
            self._build_group_controls(group, items)

        self._toggle_group('Curve', True)
        self._toggle_group('Mosh', True)
        self._toggle_group('Output', True)
        self._toggle_group('Pixel Sort', True)
        self._toggle_group('Smoothing', True)
        self._toggle_group('Subject Protect', True)

        self.controls_content = ttk.Frame(frame)
        self.controls_content.grid(row=23, column=0, columnspan=3, sticky='nsew', pady=(8, 0))
        self.controls_content.configure(style='Panel.TFrame')

        self.status_var = tk.StringVar(value='Ready')
        ttk.Label(self.controls_content, textvariable=self.status_var).pack(anchor='w', pady=(0, 6))
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(self.controls_content, orient='horizontal', mode='determinate', variable=self.progress_var, maximum=100, style='TProgressbar')
        self.progress.pack(fill='x', pady=(0, 8))

        self.vividness_frame = ttk.LabelFrame(self.controls_content, text='Vividness', padding=8)
        self.vividness_frame.pack(fill='x', pady=(0, 8))
        self.vividness_frame.configure(style='Section.TLabelframe')
        self.vividness_canvas = tk.Canvas(self.vividness_frame, width=1, height=90, bg='#1b1a18', highlightthickness=0)
        self.vividness_canvas.pack(fill='both', expand=True)
        self.vividness_canvas.bind('<Configure>', self._draw_vividness_preview)
        self._draw_vividness_preview()
        self.after(100, self._animate_vividness_preview)

        self.log = tk.Text(self.controls_content, height=6, width=90)
        self.log.pack(fill='both', expand=True, pady=(0, 8))
        self.log.configure(state='normal')
        self.log.insert('1.0', 'Run status will appear here.\n')
        self.log.configure(state='disabled')
        self._apply_styles()

        self.preview_panel = ttk.LabelFrame(self.controls_content, text='Preview export', padding=8)
        self.preview_panel.pack(fill='x', pady=(0, 8))
        self.preview_panel.configure(style='Section.TLabelframe')
        ttk.Label(self.preview_panel, text='Render a short preview file instead of an in-app player.', foreground='#555').pack(anchor='w')
        ttk.Label(self.preview_panel, text='The preview is written as a .preview.mp4 next to your input video.', foreground='#777').pack(anchor='w', pady=(4, 0))

        self.preset_frame = ttk.Frame(self.controls_content)
        self.preset_frame.pack(fill='x', pady=(0, 8))
        ttk.Label(self.preset_frame, text='Preset').pack(side='left', padx=(0, 4))
        preset_combo = ttk.Combobox(self.preset_frame, textvariable=self.preset_slot_var, values=list(PRESET_SLOTS),
                                     state='readonly', width=3)
        preset_combo.pack(side='left', padx=(0, 6))
        ttk.Button(self.preset_frame, text='Save', command=self._save_preset, style='Secondary.TButton', width=8).pack(side='left', padx=(0, 4))
        ttk.Button(self.preset_frame, text='Load', command=self._load_preset, style='Secondary.TButton', width=8).pack(side='left', padx=(0, 16))
        ttk.Button(self.preset_frame, text='Reset to Default', command=self._reset_to_defaults, style='Secondary.TButton').pack(side='left')

        self.action_frame = ttk.Frame(self.controls_content)
        self.action_frame.pack(fill='x')
        self.preview_button = ttk.Button(self.action_frame, text='Render 15s Preview', command=self._run_preview, width=20, style='Primary.TButton')
        self.preview_button.pack(side='left', padx=(0, 8))
        self.run_button = ttk.Button(self.action_frame, text='Run', command=self._run_pipeline, width=14, style='Run.TButton')
        self.run_button.pack(side='left')

        self.controls_content.columnconfigure(0, weight=1)
        self.bind('<Configure>', self._handle_window_resize)
        self.after(50, self._handle_window_resize)

    def _apply_styles(self):
        theme_name = self.theme_var.get() if hasattr(self, 'theme_var') and self.theme_var.get() else 'ember'
        palette = THEMES.get(theme_name, THEMES['ember'])
        style = ttk.Style(self)
        style.theme_use('clam')
        css_rules = self._load_css_rules()

        def _get_props(selector):
            return css_rules.get(selector, {})

        self.configure(bg=palette['root_bg'])

        style.configure('Panel.TFrame', background=palette['panel_bg'])
        style.configure('Section.TLabelframe', background=palette['section_bg'], foreground=palette['text_fg'],
                        bordercolor=palette.get('section_border', palette['section_bg']), borderwidth=2, relief='solid')
        style.configure('Section.TLabelframe.Label', background=palette['section_bg'], foreground=palette['text_fg'], font=('Segoe UI', 11, 'bold'))

        title_props = _get_props('.title')
        if title_props:
            style.configure('Title.TLabel', background=palette['panel_bg'], foreground=title_props.get('foreground', palette['text_fg']), font=self._font_from_css(title_props))

        control_props = _get_props('.control-label')
        if control_props:
            style.configure('Control.TLabel', background=palette['panel_bg'], foreground=control_props.get('foreground', palette['text_fg']), font=self._font_from_css(control_props))

        desc_props = _get_props('.description')
        if desc_props:
            style.configure('Description.TLabel', background=palette['panel_bg'], foreground=desc_props.get('foreground', palette['muted_fg']), font=self._font_from_css(desc_props))

        style.configure('Entry.TEntry', fieldbackground=palette['entry_bg'], foreground=palette['entry_fg'], font=('Segoe UI', 10))
        style.map('Entry.TEntry', fieldbackground=[('active', palette['entry_active_bg'])], foreground=[('disabled', '#888888')])

        style.configure('Primary.TButton', background='#f2b24d', foreground='#16110c', font=('Segoe UI', 10, 'bold'), padding=(8, 10))
        style.map('Primary.TButton', background=[('active', '#7fa4e5'), ('pressed', '#6d8ed0')], foreground=[('active', '#ffffff'), ('pressed', '#ffffff')])

        style.configure('Secondary.TButton', background=palette['button_bg'], foreground=palette['button_fg'], font=('Segoe UI', 10, 'bold'), padding=(8, 10))
        style.map('Secondary.TButton', background=[('active', palette['button_active_bg']), ('pressed', palette['button_active_bg'])], foreground=[('active', palette['button_active_fg']), ('pressed', palette['button_active_fg'])])

        style.configure('Run.TButton', background='#2f8f45', foreground='#f7fbf2', font=('Segoe UI', 10, 'bold'), padding=(8, 10))
        style.map('Run.TButton', background=[('active', '#3fae56'), ('pressed', '#287635')], foreground=[('active', '#ffffff'), ('pressed', '#ffffff')])

        style.configure('Status.TLabel', background=palette['panel_bg'], foreground=palette['text_fg'], font=('Segoe UI', 10, 'bold'))
        style.configure('TProgressbar', background=palette['progress_fg'], troughcolor=palette['progress_bg'])

        style.configure('TFrame', background=palette['panel_bg'])
        style.configure('TLabel', background=palette['panel_bg'], foreground=palette['text_fg'], font=('Segoe UI', 10))
        style.configure('TEntry', fieldbackground=palette['entry_bg'], foreground=palette['entry_fg'])
        style.map('TEntry', fieldbackground=[('active', palette['entry_active_bg'])], foreground=[('disabled', '#888888')])
        style.configure('TButton', background=palette['button_bg'], foreground=palette['button_fg'], font=('Segoe UI', 10, 'bold'))
        style.map('TButton', background=[('active', palette['button_active_bg'])], foreground=[('disabled', '#888888')])
        style.configure('TCheckbutton', background=palette['panel_bg'], foreground=palette['text_fg'], font=('Segoe UI', 10))
        style.map('TCheckbutton', background=[('active', palette['check_hover_bg'])], foreground=[('active', palette['check_hover_fg']), ('selected', palette['text_fg'])])
        style.configure('TScrollbar', background=palette['button_bg'], troughcolor=palette['panel_bg'])

        if hasattr(self, 'log'):
            self.log.configure(bg=palette['log_bg'], fg=palette['log_fg'], insertbackground=palette['log_fg'], relief='flat')
        if hasattr(self, 'vividness_canvas'):
            self.vividness_canvas.configure(bg=palette['canvas_bg'])
            self._draw_vividness_preview()
        if hasattr(self, 'controls_content'):
            self.controls_content.configure(style='Panel.TFrame')
        self._handle_window_resize()

    def _load_css_rules(self):
        if not STYLE_PATH.exists():
            return {}
        rules = {}
        current_selector = None
        for raw_line in STYLE_PATH.read_text(encoding='utf-8').splitlines():
            line = raw_line.split('//', 1)[0].strip()
            if not line:
                continue
            if line.endswith('{'):
                current_selector = line[:-1].strip()
                rules[current_selector] = {}
            elif line.endswith('}'):
                current_selector = None
            elif current_selector and ':' in line:
                prop, value = [part.strip() for part in line.split(':', 1)]
                if value.endswith(';'):
                    value = value[:-1]
                rules[current_selector][prop] = value
        return rules

    def _font_from_css(self, props):
        family = props.get('font-family', 'Segoe UI')
        size = int(props.get('font-size', '10'))
        weight = props.get('font-weight', 'normal').lower()
        if weight == 'bold':
            return (family, size, 'bold')
        return (family, size)

    def _pick_input(self):
        path = filedialog.askopenfilename(filetypes=[('Video files', '*.mp4 *.mov *.mkv *.avi *.webm *.m4v')])
        if path:
            self.input_var.set(path)
            self.input_duration_var.set(self._get_video_duration(path))
            self.size_warning_var.set('')
            worker = threading.Thread(target=self._check_intermediate_size, args=(path,), daemon=True)
            worker.start()

    def _check_intermediate_size(self, path):
        try:
            quality = int(float(self.quality_var.get()))
            keyframe_interval = int(float(self.keyframe_interval_var.get())) if self.force_keyframe_interval_var.get() else 9999
            presort = None
            if bool(self.pixel_sort_var.get()) and self.pixel_sort_timing_var.get() == 'before-mosh':
                presort = {
                    'direction': self.pixel_sort_direction_var.get(),
                    'aggression': float(self.pixel_sort_aggression_var.get()),
                    'key': self.pixel_sort_key_var.get(),
                }
            plan = estimate_segment_plan(path, quality, keyframe_interval, presort_pixel_sort=presort)
        except Exception:
            return
        if not plan or plan[2] <= 1:
            return
        estimated, _duration, chunk_count = plan
        gb = estimated / (1024 ** 3)
        message = (f'ℹ Estimated intermediate ~{gb:.1f}GB at the current Quality setting, past the ~1GB '
                   f'single-segment limit — will run automatically as {chunk_count} segments stitched back '
                   'together. Takes longer; a higher Quality number (more compression) avoids it entirely.')
        self.after(0, lambda: self.size_warning_var.set(message))

    def _pick_output(self):
        path = filedialog.asksaveasfilename(defaultextension='.mp4', filetypes=[('MP4', '*.mp4'), ('WebM', '*.webm')])
        if path:
            self.output_var.set(path)

    def _format_duration(self, seconds):
        try:
            total = max(0, int(float(seconds)))
        except (TypeError, ValueError):
            return 'Duration unavailable'
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f'Length: {hours:d}:{minutes:02d}:{secs:02d}'
        return f'Length: {minutes:d}:{secs:02d}'

    def _get_video_duration(self, path):
        if not path:
            return ''
        ffprobe = shutil.which('ffprobe')
        if not ffprobe:
            return 'Duration unavailable'
        try:
            result = subprocess.run(
                [ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', path],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return 'Duration unavailable'
            return self._format_duration(result.stdout.strip())
        except Exception:
            return 'Duration unavailable'

    def _build_group_controls(self, group, items):
        for item in items:
            label = item['label']
            variable = item['variable']
            kind = item.get('kind', 'slider')

            row = ttk.Frame(group)
            row.pack(fill='x', pady=(4, 2))
            row.columnconfigure(1, weight=1)
            ttk.Label(row, text=label, width=24, anchor='w').grid(row=0, column=0, sticky='w', padx=(0, 8))

            if kind == 'entry':
                ttk.Entry(row, textvariable=variable, width=16).grid(row=0, column=1, sticky='ew', padx=(0, 6))
            elif kind == 'combo':
                combo = ttk.Combobox(row, textvariable=variable, values=item['values'], state='readonly', width=14)
                combo.grid(row=0, column=1, sticky='w', padx=(0, 6))
            else:
                scale = ttk.Scale(row, from_=item['from_'], to=item['to'], orient='horizontal')
                scale.grid(row=0, column=1, sticky='ew', padx=(0, 6))
                value_var = tk.StringVar(value=self._format_slider_value(variable.get(), item['step']))
                scale.set(float(variable.get()))
                scale.configure(command=lambda value, var=variable, disp=value_var, step=item['step']: self._set_slider_value(value, var, disp, step))
                self._set_slider_value(scale.get(), variable, value_var, item['step'])
                ttk.Label(row, textvariable=value_var, width=8, anchor='e').grid(row=0, column=2, sticky='e')

            description = item.get('description', '')
            if description:
                desc_label = ttk.Label(group, text=description, foreground='#666', wraplength=320, justify='left')
                desc_label.pack(fill='x', padx=(24, 0), pady=(0, 4))
                self._description_labels.append(desc_label)

    def _toggle_group(self, name, expanded):
        group = self.group_frames.get(name)
        if not group:
            return
        for child in group.winfo_children():
            if expanded:
                child.pack(fill='x') if child.winfo_manager() == 'pack' else None
            else:
                child.pack_forget()

    def _handle_window_resize(self, event=None):
        # Debounced: Windows fires a burst of Configure events during a live
        # resize drag (and even just when the window regains focus after a
        # file dialog closes) — reconfiguring every description label
        # synchronously on each one blocked the event loop long enough to
        # show a brief unstyled/black flash before ttk caught up. Only do
        # the real work once resize activity has settled.
        if not hasattr(self, '_description_labels'):
            return
        if self._resize_after_id is not None:
            self.after_cancel(self._resize_after_id)
        self._resize_after_id = self.after(120, self._apply_resize_wraplength)

    def _apply_resize_wraplength(self):
        self._resize_after_id = None
        width = max(280, self.winfo_width() - 180)
        for label in self._description_labels:
            if label.winfo_exists():
                label.configure(wraplength=max(220, width))

    def _format_slider_value(self, value, step):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return str(value)
        if step is None:
            return str(value)
        if step >= 1:
            return str(int(round(numeric)))
        return str(round(numeric, 2))

    def _set_slider_value(self, value, variable, display_var, step):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        if step is None:
            text = str(value)
        elif step >= 1:
            text = str(int(round(numeric)))
        else:
            text = str(round(numeric, 2))
        variable.set(text)
        display_var.set(text)

    def _draw_vividness_preview(self, event=None):
        self.vividness_canvas.delete('all')
        width = event.width if event is not None else self.vividness_canvas.winfo_width()
        width = max(1, int(width if width > 1 else 320))
        height = 90
        mid_y = height / 2
        cycles = float(self.cycles_var.get())
        motion_weight = float(self.motion_weight_var.get())
        audio_weight = float(self.audio_weight_var.get())

        sample_count = max(24, min(96, width // 6))
        motion_energy = [0.5 + 0.5 * math.sin(2.0 * math.pi * 0.35 * (i / 8.0)) for i in range(sample_count)]
        audio_energy = [0.5 + 0.5 * math.sin(2.0 * math.pi * 0.15 * (i / 10.0) + 1.2) for i in range(sample_count)]
        curve = build_vividness_curve(sample_count, motion_energy=motion_energy, audio_energy=audio_energy,
                                      cycles=cycles, motion_weight=motion_weight, audio_weight=audio_weight)
        forget_curve = build_forgetting_curve(sample_count, cycles=cycles)

        palette = THEMES.get(self.theme_var.get(), THEMES['ember'])
        self.vividness_canvas.create_rectangle(0, 0, width, height, fill=palette['canvas_bg'], outline='')
        self.vividness_canvas.create_line(10, mid_y, width - 10, mid_y, fill=palette['curve_line'], width=1)

        def _point(idx, value, count):
            px = 10 + int(idx * (width - 20) / max(count - 1, 1))
            py = mid_y - (value - 0.5) * 28
            return px, py

        forget_points = [coord for idx, value in enumerate(forget_curve) for coord in _point(idx, value, len(forget_curve))]
        if len(forget_points) >= 4:
            self.vividness_canvas.create_line(*forget_points, fill=palette['forget_curve'], width=2, smooth=True)

        for idx, value in enumerate(curve):
            px, py = _point(idx, value, len(curve))
            color = palette['curve_peak'] if value > 0.65 else palette['curve_low']
            self.vividness_canvas.create_oval(px - 2, py - 2, px + 2, py + 2, fill=color, outline='')

        self.vividness_canvas.create_text(20, 72, anchor='nw', text='● vividness (motion + wave)', fill=palette['curve_peak'], font=('Helvetica', 9))
        self.vividness_canvas.create_text(max(180, width - 170), 72, anchor='nw', text='— forgetting curve (power-law)', fill=palette['forget_curve'], font=('Helvetica', 9))

    def _animate_vividness_preview(self):
        self._draw_vividness_preview()
        self.after(120, self._animate_vividness_preview)

    def _append_log(self, message, fraction=None):
        self.after(0, lambda: self._append_log_now(message, fraction))

    def _append_log_now(self, message, fraction=None):
        self.log.configure(state='normal')
        self.log.insert('end', message + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')
        if fraction is not None:
            self.progress_var.set(max(0.0, min(100.0, fraction * 100.0)))

    def _run_preview(self):
        if not self.input_var.get():
            messagebox.showerror('Missing input', 'Choose an input video first.')
            return
        try:
            duration = int(float(self.preview_var.get()))
        except ValueError:
            messagebox.showerror('Invalid preview length', 'Preview duration must be an integer number of seconds.')
            return

        config = self._build_config()
        if not config['input'] or not config['output']:
            messagebox.showerror('Missing paths', 'Choose an input video first and confirm the output location.')
            return

        self.status_var.set('Rendering preview…')
        self.progress_var.set(0.0)
        self._append_log(f'Generating a {duration}s preview…')
        worker = threading.Thread(target=self._run_preview_worker, args=(config, duration), daemon=True)
        worker.start()

    def _run_preview_worker(self, config, duration):
        try:
            preview_config = build_preview_config(config, duration)
            preview_config['output'] = str(Path(config['input']).with_suffix('.preview.mp4'))
            preview_config['keep_intermediate'] = True
            preview_config['preview_duration'] = duration
            result = run_pipeline_auto(preview_config, progress_callback=self._append_log)
            self.after(0, lambda: self._finish_preview(result['output_path']))
        except Exception as exc:
            self.after(0, lambda: self._fail_run(exc))

    def _finish_preview(self, preview_path):
        self.status_var.set('Preview ready')
        self.progress_var.set(100.0)
        self._append_log(f'Preview written to {preview_path}')
        self._show_preview(preview_path)

    def _show_preview(self, preview_path):
        self._append_log(f'Preview export complete: {preview_path}')
        self._append_log('Open the preview file to review the result.')

    def _build_config(self):
        return {
            'input': self.input_var.get(),
            'output': self.output_var.get(),
            'use_vividness_curve': bool(self.use_curve_var.get()),
            'include_audio': bool(self.audio_var.get()),
            'analysis_fps': float(self.analysis_fps_var.get()),
            'curve_cycles': float(self.cycles_var.get()),
            'motion_weight': float(self.motion_weight_var.get()),
            'audio_weight': float(self.audio_weight_var.get()),
            'keyframe_interval': int(float(self.keyframe_interval_var.get())) if self.force_keyframe_interval_var.get() else 9999,
            'keyframe_removal_rate': float(self.keyframe_rate_var.get()),
            'duplicate_rate': float(self.duplicate_rate_var.get()),
            'duplicate_min': int(float(self.duplicate_repeat_min_var.get())),
            'duplicate_max': int(float(self.duplicate_repeat_max_var.get())),
            'freeze_chance': float(self.freeze_chance_var.get()),
            'freeze_min': int(self.freeze_min_var.get()),
            'freeze_max': int(self.freeze_max_var.get()),
            'quality': int(float(self.quality_var.get())),
            'seed': int(self.seed_var.get()) if self.seed_var.get() else None,
            'keep_intermediate': False,
            'pixel_sort_enabled': bool(self.pixel_sort_var.get()),
            'pixel_sort_direction': self.pixel_sort_direction_var.get(),
            'pixel_sort_key': self.pixel_sort_key_var.get(),
            'pixel_sort_timing': self.pixel_sort_timing_var.get(),
            'pixel_sort_aggression': float(self.pixel_sort_aggression_var.get()),
            'temporal_blend_enabled': bool(self.temporal_blend_var.get()),
            'temporal_blend_mode': self.temporal_blend_mode_var.get(),
            'subject_protect_enabled': bool(self.subject_protect_var.get()),
            'subject_protect_channel': self.subject_protect_channel_var.get(),
            'subject_protect_low': float(self.subject_protect_low_var.get()),
            'subject_protect_high': float(self.subject_protect_high_var.get()),
        }

    def _run_pipeline(self):
        try:
            config = self._build_config()
        except ValueError as exc:
            messagebox.showerror('Invalid input', str(exc))
            return

        if not config['input'] or not config['output']:
            messagebox.showerror('Missing paths', 'Choose both an input and output path first.')
            return

        # Persist before starting, not after — if the machine gets forced
        # to reboot mid-render (Windows Update did this twice already),
        # the settings that produced whatever's on disk are still known.
        self._save_last_used()

        self.status_var.set('Working…')
        self.progress_var.set(0.0)
        self._append_log('Starting render…')
        worker = threading.Thread(target=self._run_worker, args=(config,), daemon=True)
        worker.start()

    def _run_worker(self, config):
        try:
            result = run_pipeline_auto(config, progress_callback=self._append_log)
            self.after(0, lambda: self._finish_run(result))
        except Exception as exc:
            self.after(0, lambda: self._fail_run(exc))

    def _finish_run(self, result):
        self.status_var.set('Completed')
        self.progress_var.set(100.0)
        self._append_log(f"Finished -> {result['output_path']}")
        segment_note = f" (processed as {result['segment_count']} segments)" if result.get('segmented') else ''
        messagebox.showinfo('Done', f"Completed with {result['keyframes_removed']} keyframes removed and "
                                     f"{result['frames_duplicated']} duplicated frames.{segment_note}")

    def _fail_run(self, exc):
        self.status_var.set('Failed')
        if isinstance(exc, MemoryError):
            message = 'The render ran out of memory while processing the video.'
        else:
            message = str(exc) if str(exc).strip() else repr(exc)
        self._append_log(f'Error: {message}')
        if not message.strip():
            message = 'The render failed, but no detailed error message was returned.'
        messagebox.showerror('Render failed', message)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description='Real I-frame-removal / delta-frame-duplication datamoshing.')
    p.add_argument('input', nargs='?', help='source video file')
    p.add_argument('output', nargs='?', help='output video file (.mp4 or .webm)')
    p.add_argument('--keyframe-interval', type=int, default=9999,
                   help='frames between forced keyframes in the intermediate transcode (default 9999, i.e. '
                        'effectively none beyond the first — lower it, e.g. 30-60, to give keyframe removal real '
                        'material on footage with no real scene cuts)')
    p.add_argument('--keyframe-removal-rate', type=float, default=0.9,
                   help='probability a keyframe after the first is removed (0-1, default 0.9)')
    p.add_argument('--duplicate-rate', type=float, default=0.15,
                   help='probability a delta frame is duplicated a few times (0-1, default 0.15)')
    p.add_argument('--duplicate-min', type=int, default=2)
    p.add_argument('--duplicate-max', type=int, default=4)
    p.add_argument('--freeze-chance', type=float, default=0.02,
                   help='probability of a much longer freeze/drag (0-1, default 0.02)')
    p.add_argument('--freeze-min', type=int, default=6)
    p.add_argument('--freeze-max', type=int, default=18)
    p.add_argument('--quality', type=int, default=3, help='ffmpeg mpeg4 -q:v for the intermediate encode (1=best, 31=worst, default 3)')
    p.add_argument('--seed', type=int, default=None, help='random seed, for reproducible results')
    p.add_argument('--keep-intermediate', action='store_true', help='keep the raw and moshed .avi files instead of deleting them')
    p.add_argument('--no-vividness-curve', dest='use_vividness_curve', action='store_false', default=True,
                   help='disable the motion-driven vividness curve and fall back to flat rates')
    p.add_argument('--analysis-fps', type=float, default=4.0, help='fps used to sample the source for the vividness curve')
    p.add_argument('--curve-cycles', type=float, default=1.5, help='number of vividness waves across the clip')
    p.add_argument('--motion-weight', type=float, default=0.55, help='how strongly motion influences the vividness curve')
    p.add_argument('--audio-weight', type=float, default=0.15, help='how strongly audio energy influences the vividness curve')
    p.add_argument('--audio-analysis', action='store_true', help='include audio energy in the vividness curve')
    p.add_argument('--pixel-sort', dest='pixel_sort_enabled', action='store_true',
                   help='apply a threshold-interval pixel sort over the moshed output (requires pillow + numpy)')
    p.add_argument('--pixel-sort-direction', choices=['rows', 'cols', 'both'], default='both',
                   help='axis to sort pixel runs along (default both: rows then columns)')
    p.add_argument('--pixel-sort-key', choices=['brightness', 'hue', 'saturation', 'lightness'], default='brightness',
                   help='pixel property used for eligibility + ordering (default brightness)')
    p.add_argument('--pixel-sort-aggression', type=float, default=0.5,
                   help='0-1, how wide the sort-key window is that gets sorted (default 0.5)')
    p.add_argument('--pixel-sort-timing', choices=['after-mosh', 'before-mosh'], default='after-mosh',
                   help='"after-mosh" (default): sort and mosh corruption stay separate layers. "before-mosh": '
                        'sorted footage gets moshed too, fusing both into one rougher texture')
    p.add_argument('--smooth', dest='temporal_blend_enabled', action='store_true',
                   help='motion-aware smoothing (ffmpeg minterpolate) to soften stutter/flicker, runs last')
    p.add_argument('--smooth-mode', choices=['blend', 'motion'], default='blend',
                   help='"blend": gentler motion-compensated blending. "motion": full motion-compensated '
                        'interpolation, stronger but more prone to warping around sharp jumps (default blend)')
    p.add_argument('--subject-protect', dest='subject_protect_enabled', action='store_true',
                   help='keep pixels in a hue/saturation/lightness/brightness range moving normally (pulled from '
                        'the clean source) while everything else freezes/drags/glitches as usual')
    p.add_argument('--subject-protect-channel', choices=['hue', 'saturation', 'lightness', 'brightness'],
                   default='hue', help='which property defines the protected range (default hue)')
    p.add_argument('--subject-protect-low', type=float, default=0.0,
                   help='low end of the protected range, 0-255 scale (default 0)')
    p.add_argument('--subject-protect-high', type=float, default=60.0,
                   help='high end of the protected range, 0-255 scale (default 60)')
    return p.parse_args(argv)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        app = MemoryMoshApp()
        app.mainloop()
        return

    args = parse_args(argv)
    if not args.input or not args.output:
        print('Usage: python memory_mosh.py INPUT OUTPUT [options]', file=sys.stderr)
        sys.exit(1)

    config = {
        'input': args.input,
        'output': args.output,
        'use_vividness_curve': args.use_vividness_curve,
        'include_audio': args.audio_analysis,
        'analysis_fps': args.analysis_fps,
        'curve_cycles': args.curve_cycles,
        'motion_weight': args.motion_weight,
        'audio_weight': args.audio_weight,
        'keyframe_interval': args.keyframe_interval,
        'keyframe_removal_rate': args.keyframe_removal_rate,
        'duplicate_rate': args.duplicate_rate,
        'duplicate_min': args.duplicate_min,
        'duplicate_max': args.duplicate_max,
        'freeze_chance': args.freeze_chance,
        'freeze_min': args.freeze_min,
        'freeze_max': args.freeze_max,
        'quality': args.quality,
        'seed': args.seed,
        'keep_intermediate': args.keep_intermediate,
        'pixel_sort_enabled': args.pixel_sort_enabled,
        'pixel_sort_direction': args.pixel_sort_direction,
        'pixel_sort_key': args.pixel_sort_key,
        'pixel_sort_aggression': args.pixel_sort_aggression,
        'pixel_sort_timing': args.pixel_sort_timing,
        'temporal_blend_enabled': args.temporal_blend_enabled,
        'temporal_blend_mode': args.smooth_mode,
        'subject_protect_enabled': args.subject_protect_enabled,
        'subject_protect_channel': args.subject_protect_channel,
        'subject_protect_low': args.subject_protect_low,
        'subject_protect_high': args.subject_protect_high,
    }

    plan = estimate_segment_plan(args.input, args.quality, args.keyframe_interval,
                                 presort_pixel_sort=presort_pixel_sort_config(config))
    if plan and plan[2] > 1:
        estimated, _duration, chunk_count = plan
        gb = estimated / (1024 ** 3)
        print(f'Note: estimated intermediate ~{gb:.1f}GB at this quality exceeds the ~1GB single-segment '
              f'limit — will be processed automatically as {chunk_count} segments and stitched back together.',
              file=sys.stderr)

    result = run_pipeline_auto(config)
    print(f"done -> {result['output_path']}")


if __name__ == '__main__':
    main()
