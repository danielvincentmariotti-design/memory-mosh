#!/usr/bin/env python3
"""
memory_mosh.py — real datamoshing, desktop edition.

Pipeline:
  1. Transcode the input to AVI/mpeg4 with a near-infinite keyframe
     interval (so the source material is mostly one long run of delta
     frames — the raw stuff datamoshing actually operates on).
  2. Remove keyframes after the first (real I-frame removal) and
     duplicate selected delta frames (real motion-vector dragging),
     directly on the encoded bitstream — see avimosh.py.
  3. Re-encode the moshed AVI through ffmpeg's own (permissive) decoder
     into a normal, shareable MP4/WebM. The corruption is baked into
     the pixels at this point, so the output plays everywhere.

Requires only ffmpeg on PATH. No pip dependencies.
"""

import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from tkinter import Scrollbar

STYLE_PATH = Path(__file__).with_name('styles.css')

try:
    import PIL.Image
    import PIL.ImageTk
except ImportError:
    PIL = None

import avimosh
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
    },
    'light': {
        'root_bg': '#f3f5f9',
        'panel_bg': '#ffffff',
        'section_bg': '#f8f9fc',
        'text_fg': '#1f2937',
        'muted_fg': '#667085',
        'entry_bg': '#ffffff',
        'entry_fg': '#111827',
        'entry_active_bg': '#f2f5ff',
        'button_bg': '#4b6cb7',
        'button_fg': '#ffffff',
        'button_active_bg': '#3f5f9d',
        'button_active_fg': '#ffffff',
        'check_hover_bg': '#dce7ff',
        'check_hover_fg': '#1f2937',
        'progress_bg': '#e5eaf2',
        'progress_fg': '#2f8f45',
        'log_bg': '#f8fafc',
        'log_fg': '#0f172a',
        'canvas_bg': '#f4f1ea',
        'curve_line': '#c2b8a3',
        'curve_peak': '#d08c2f',
        'curve_low': '#9b7b4c',
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
    },
}


def run_ffmpeg(args, label):
    cmd = ['ffmpeg', '-y', *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f'\n--- ffmpeg failed during: {label} ---', file=sys.stderr)
        print(result.stderr[-2000:], file=sys.stderr)
        sys.exit(1)


def build_preview_ffmpeg_args(input_path, output_path, duration=20):
    return ['-i', str(input_path), '-t', str(duration), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(output_path)]


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
                          cycles=1.5, motion_weight=0.55, audio_weight=0.15):
    if length <= 1:
        return [0.5]

    motion_energy = resample_series(motion_energy or [0.0] * length, length)
    audio_energy = resample_series(audio_energy or [0.0] * length, length)
    motion_norm = normalize_series(motion_energy)
    audio_norm = normalize_series(audio_energy)

    curve = []
    for i in range(length):
        t = i / max(length - 1, 1)
        base_wave = 0.5 + 0.5 * math.sin(2.0 * math.pi * cycles * t)
        blended = ((1.0 - motion_weight - audio_weight) * base_wave +
                   motion_weight * motion_norm[i] +
                   audio_weight * audio_norm[i])
        curve.append(clamp(blended))

    curve[0] = curve[-1] = (curve[0] + curve[-1]) / 2.0
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
    analysis_dir = Path(tempfile.mkdtemp(prefix='memory-mosh-analysis-'))
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
                            include_audio=False):
    motion_energy = analyze_motion_energy(input_path, target_length, analysis_fps=analysis_fps)
    audio_energy = []
    if include_audio:
        audio_energy = analyze_audio_energy(input_path, target_length)
    return build_vividness_curve(target_length, motion_energy=motion_energy,
                                 audio_energy=audio_energy,
                                 cycles=cycles,
                                 motion_weight=motion_weight,
                                 audio_weight=audio_weight)


def run_pipeline(config, progress_callback=None):
    if shutil.which('ffmpeg') is None:
        raise RuntimeError('ffmpeg not found on PATH. Install it from https://ffmpeg.org/download.html')

    in_path = Path(config['input'])
    out_path = Path(config['output'])
    if not in_path.exists():
        raise FileNotFoundError(f'input file not found: {in_path}')

    workdir = Path(tempfile.mkdtemp(prefix='memory-mosh-')) if not config.get('keep_intermediate', False) else out_path.parent
    raw_avi = workdir / f'{in_path.stem}_raw.avi'
    moshed_avi = workdir / f'{in_path.stem}_moshed.avi'

    source_path = in_path
    if config.get('preview_duration'):
        preview_path = workdir / f'{in_path.stem}_preview.mp4'
        if progress_callback:
            progress_callback(f'Creating a {config["preview_duration"]}s preview clip…')
        run_ffmpeg(['-i', str(in_path), '-t', str(config['preview_duration']), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-an', str(preview_path)], 'preview-trim')
        source_path = preview_path

    if progress_callback:
        progress_callback('Transcoding to a raw AVI/mpeg4 intermediate…')
    run_ffmpeg(['-i', str(source_path), '-c:v', 'mpeg4', '-g', '9999', '-bf', '0',
                '-q:v', str(config['quality']), '-an', str(raw_avi)], 'transcode')

    raw_data = raw_avi.read_bytes()
    _, _, movi, _ = avimosh.parse_avi(raw_data)
    frame_count = len(avimosh.parse_movi_frames(raw_data, movi))

    vividness_curve = None
    if config.get('use_vividness_curve', False):
        if progress_callback:
            progress_callback('Analyzing motion into a vividness curve…')
        vividness_curve = analyze_vividness_curve(
            in_path,
            target_length=frame_count,
            analysis_fps=config.get('analysis_fps', 4.0),
            cycles=config.get('curve_cycles', 1.5),
            motion_weight=config.get('motion_weight', 0.55),
            audio_weight=config.get('audio_weight', 0.15),
            include_audio=config.get('include_audio', False),
        )

    if progress_callback:
        progress_callback('Removing keyframes and duplicating delta frames…')
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
        progress_callback('Re-encoding to a shareable output file…')
    if out_path.suffix.lower() == '.webm':
        codec_args = ['-c:v', 'libvpx-vp9', '-crf', '30', '-b:v', '0']
    else:
        codec_args = ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18']
    run_ffmpeg(['-i', str(moshed_avi), *codec_args, str(out_path)], 're-encode')

    if not config.get('keep_intermediate', False):
        shutil.rmtree(workdir, ignore_errors=True)

    stats['output_path'] = str(out_path)
    return stats


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
        self.use_curve_var = tk.BooleanVar(value=True)
        self.audio_var = tk.BooleanVar(value=False)
        self.analysis_fps_var = tk.StringVar(value='4.0')
        self.cycles_var = tk.StringVar(value='1.5')
        self.motion_weight_var = tk.StringVar(value='0.55')
        self.audio_weight_var = tk.StringVar(value='0.15')
        self.keyframe_rate_var = tk.StringVar(value='0.9')
        self.duplicate_rate_var = tk.StringVar(value='0.15')
        self.block_size_var = tk.StringVar(value='4')
        self.freeze_chance_var = tk.StringVar(value='0.02')
        self.freeze_min_var = tk.StringVar(value='6')
        self.freeze_max_var = tk.StringVar(value='18')
        self.quality_var = tk.StringVar(value='3')
        self.seed_var = tk.StringVar(value='')
        self.preview_var = tk.StringVar(value='15')
        self.theme_var = tk.StringVar(value='dark')
        self._description_labels = []

        self._build_ui()

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
        ttk.Label(frame, textvariable=self.input_duration_var, style='Description.TLabel').grid(row=3, column=1, sticky='w', padx=6, pady=(0, 4))

        ttk.Label(frame, text='Output video', style='Control.TLabel').grid(row=4, column=0, sticky='w', pady=(0, 4))
        ttk.Entry(frame, textvariable=self.output_var, width=44, style='Entry.TEntry').grid(row=4, column=1, sticky='ew', padx=6, pady=(0, 4))
        ttk.Button(frame, text='Browse', command=self._pick_output, style='Secondary.TButton').grid(row=4, column=2, padx=4, pady=(0, 4), sticky='ew')

        checkbox_row = ttk.Frame(frame)
        checkbox_row.grid(row=5, column=1, sticky='w', pady=(6, 2))
        checkbox_row.columnconfigure(0, weight=1)
        checkbox_row.columnconfigure(1, weight=1)
        ttk.Checkbutton(checkbox_row, text='Use vividness curve', variable=self.use_curve_var).grid(row=0, column=0, sticky='w', padx=(0, 16))
        ttk.Checkbutton(checkbox_row, text='Analyze audio energy', variable=self.audio_var).grid(row=0, column=1, sticky='w')

        effect_groups = [
            ('Curve', [
                {'label': 'Analysis fps', 'variable': self.analysis_fps_var, 'from_': 1.0, 'to': 10.0, 'step': 0.5, 'description': 'How densely the source is sampled for the vividness curve.'},
                {'label': 'Curve cycles', 'variable': self.cycles_var, 'from_': 0.5, 'to': 6.0, 'step': 0.1, 'description': 'How many wave cycles sweep across the clip.'},
                {'label': 'Motion weight', 'variable': self.motion_weight_var, 'from_': 0.0, 'to': 1.0, 'step': 0.05, 'description': 'How strongly motion influences the intensity curve.'},
                {'label': 'Audio weight', 'variable': self.audio_weight_var, 'from_': 0.0, 'to': 1.0, 'step': 0.05, 'description': 'How strongly audio energy nudges the curve.'},
            ], 7),
            ('Mosh', [
                {'label': 'Keyframe removal rate', 'variable': self.keyframe_rate_var, 'from_': 0.0, 'to': 1.0, 'step': 0.05, 'description': 'How often real keyframes are dropped.'},
                {'label': 'Duplicate rate', 'variable': self.duplicate_rate_var, 'from_': 0.0, 'to': 1.0, 'step': 0.05, 'description': 'How often delta frames are repeated for glitch streaks.'},
                {'label': 'Glitch block size', 'variable': self.block_size_var, 'from_': 1, 'to': 12, 'step': 1, 'description': 'How big each repeated glitch block is.'},
                {'label': 'Freeze chance', 'variable': self.freeze_chance_var, 'from_': 0.0, 'to': 0.25, 'step': 0.01, 'description': 'How often the corruption holds longer as a freeze.'},
                {'label': 'Freeze min', 'variable': self.freeze_min_var, 'from_': 1, 'to': 24, 'step': 1, 'description': 'Shortest freeze length in frames.'},
                {'label': 'Freeze max', 'variable': self.freeze_max_var, 'from_': 2, 'to': 48, 'step': 1, 'description': 'Longest freeze length in frames.'},
            ], 11),
            ('Output', [
                {'label': 'Quality', 'variable': self.quality_var, 'from_': 1, 'to': 31, 'step': 1, 'description': 'The intermediate AVI quality. Lower is cleaner.'},
                {'label': 'Preview duration (s)', 'variable': self.preview_var, 'from_': 5, 'to': 30, 'step': 1, 'description': 'How long the preview export should be.'},
                {'label': 'Seed', 'variable': self.seed_var, 'kind': 'entry', 'description': 'Optional random seed for repeatable glitches.'},
            ], 15),
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
        theme_name = self.theme_var.get() if hasattr(self, 'theme_var') and self.theme_var.get() else 'dark'
        palette = THEMES.get(theme_name, THEMES['dark'])
        style = ttk.Style(self)
        style.theme_use('clam')
        css_rules = self._load_css_rules()

        def _get_props(selector):
            return css_rules.get(selector, {})

        self.configure(bg=palette['root_bg'])

        style.configure('Panel.TFrame', background=palette['panel_bg'])
        style.configure('Section.TLabelframe', background=palette['section_bg'], foreground=palette['text_fg'])
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
            ttk.Label(row, text=label, width=18, anchor='w').grid(row=0, column=0, sticky='w', padx=(0, 8))

            if kind == 'entry':
                ttk.Entry(row, textvariable=variable, width=16).grid(row=0, column=1, sticky='ew', padx=(0, 6))
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
        if not hasattr(self, '_description_labels'):
            return
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

        palette = THEMES.get(self.theme_var.get(), THEMES['dark'])
        self.vividness_canvas.create_rectangle(0, 0, width, height, fill=palette['canvas_bg'], outline='')
        self.vividness_canvas.create_line(10, mid_y, width - 10, mid_y, fill=palette['curve_line'], width=1)
        for idx, value in enumerate(curve):
            px = 10 + int(idx * (width - 20) / max(len(curve) - 1, 1))
            py = mid_y - (value - 0.5) * 28
            color = palette['curve_peak'] if value > 0.65 else palette['curve_low']
            self.vividness_canvas.create_oval(px - 2, py - 2, px + 2, py + 2, fill=color, outline='')
        self.vividness_canvas.create_text(20, 72, anchor='nw', text='curve preview', fill='#d9d2c3', font=('Helvetica', 9))
        self.vividness_canvas.create_text(max(180, width - 140), 72, anchor='nw', text='same curve logic', fill='#8b6f47', font=('Helvetica', 9))

    def _animate_vividness_preview(self):
        self._draw_vividness_preview()
        self.after(120, self._animate_vividness_preview)

    def _append_log(self, message):
        self.after(0, lambda: self._append_log_now(message))

    def _append_log_now(self, message):
        self.log.configure(state='normal')
        self.log.insert('end', message + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

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
            result = run_pipeline(preview_config, progress_callback=self._append_log)
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
            'keyframe_removal_rate': float(self.keyframe_rate_var.get()),
            'duplicate_rate': float(self.duplicate_rate_var.get()),
            'duplicate_min': int(float(self.block_size_var.get())),
            'duplicate_max': int(float(self.block_size_var.get())),
            'freeze_chance': float(self.freeze_chance_var.get()),
            'freeze_min': int(self.freeze_min_var.get()),
            'freeze_max': int(self.freeze_max_var.get()),
            'quality': int(float(self.quality_var.get())),
            'seed': int(self.seed_var.get()) if self.seed_var.get() else None,
            'keep_intermediate': False,
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

        self.status_var.set('Working…')
        self.progress_var.set(0.0)
        self._append_log('Starting render…')
        worker = threading.Thread(target=self._run_worker, args=(config,), daemon=True)
        worker.start()

    def _run_worker(self, config):
        try:
            result = run_pipeline(config, progress_callback=self._append_log)
            self.after(0, lambda: self._finish_run(result))
        except Exception as exc:
            self.after(0, lambda: self._fail_run(exc))

    def _finish_run(self, result):
        self.status_var.set('Completed')
        self.progress_var.set(100.0)
        self._append_log(f"Finished -> {result['output_path']}")
        messagebox.showinfo('Done', f"Completed with {result['keyframes_removed']} keyframes removed and {result['frames_duplicated']} duplicated frames.")

    def _fail_run(self, exc):
        self.status_var.set('Failed')
        self._append_log(f'Error: {exc}')
        messagebox.showerror('Render failed', str(exc))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description='Real I-frame-removal / delta-frame-duplication datamoshing.')
    p.add_argument('input', nargs='?', help='source video file')
    p.add_argument('output', nargs='?', help='output video file (.mp4 or .webm)')
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
    }
    result = run_pipeline(config)
    print(f"done -> {result['output_path']}")


if __name__ == '__main__':
    main()
