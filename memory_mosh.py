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

try:
    import PIL.Image
    import PIL.ImageTk
except ImportError:
    PIL = None

import avimosh
from avimosh import rate_from_curve


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
        self.geometry('1120x820')
        self.minsize(980, 760)
        self.resizable(True, True)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
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

        frame = ttk.Frame(scroll_frame, padding=12)
        frame.pack(fill='both', expand=True)

        ttk.Label(frame, text='Input video').grid(row=0, column=0, sticky='w')
        ttk.Entry(frame, textvariable=self.input_var, width=60).grid(row=0, column=1, padx=6, pady=4)
        ttk.Button(frame, text='Browse', command=self._pick_input).grid(row=0, column=2, padx=4)

        ttk.Label(frame, text='Output video').grid(row=1, column=0, sticky='w')
        ttk.Entry(frame, textvariable=self.output_var, width=60).grid(row=1, column=1, padx=6, pady=4)
        ttk.Button(frame, text='Browse', command=self._pick_output).grid(row=1, column=2, padx=4)

        ttk.Checkbutton(frame, text='Use vividness curve', variable=self.use_curve_var).grid(row=2, column=1, sticky='w', pady=4)
        ttk.Checkbutton(frame, text='Analyze audio energy', variable=self.audio_var).grid(row=3, column=1, sticky='w', pady=4)

        effect_groups = [
            ('Curve', [
                {'label': 'Analysis fps', 'variable': self.analysis_fps_var, 'from_': 1.0, 'to': 10.0, 'step': 0.5, 'description': 'How densely the source is sampled for the vividness curve.'},
                {'label': 'Curve cycles', 'variable': self.cycles_var, 'from_': 0.5, 'to': 6.0, 'step': 0.1, 'description': 'How many wave cycles sweep across the clip.'},
                {'label': 'Motion weight', 'variable': self.motion_weight_var, 'from_': 0.0, 'to': 1.0, 'step': 0.05, 'description': 'How strongly motion influences the intensity curve.'},
                {'label': 'Audio weight', 'variable': self.audio_weight_var, 'from_': 0.0, 'to': 1.0, 'step': 0.05, 'description': 'How strongly audio energy nudges the curve.'},
            ], 4),
            ('Mosh', [
                {'label': 'Keyframe removal rate', 'variable': self.keyframe_rate_var, 'from_': 0.0, 'to': 1.0, 'step': 0.05, 'description': 'How often real keyframes are dropped.'},
                {'label': 'Duplicate rate', 'variable': self.duplicate_rate_var, 'from_': 0.0, 'to': 1.0, 'step': 0.05, 'description': 'How often delta frames are repeated for glitch streaks.'},
                {'label': 'Glitch block size', 'variable': self.block_size_var, 'from_': 1, 'to': 12, 'step': 1, 'description': 'How big each repeated glitch block is.'},
                {'label': 'Freeze chance', 'variable': self.freeze_chance_var, 'from_': 0.0, 'to': 0.25, 'step': 0.01, 'description': 'How often the corruption holds longer as a freeze.'},
                {'label': 'Freeze min', 'variable': self.freeze_min_var, 'from_': 1, 'to': 24, 'step': 1, 'description': 'Shortest freeze length in frames.'},
                {'label': 'Freeze max', 'variable': self.freeze_max_var, 'from_': 2, 'to': 48, 'step': 1, 'description': 'Longest freeze length in frames.'},
            ], 8),
            ('Output', [
                {'label': 'Quality', 'variable': self.quality_var, 'from_': 1, 'to': 31, 'step': 1, 'description': 'The intermediate AVI quality. Lower is cleaner.'},
                {'label': 'Preview duration (s)', 'variable': self.preview_var, 'from_': 5, 'to': 30, 'step': 1, 'description': 'How long the preview export should be.'},
                {'label': 'Seed', 'variable': self.seed_var, 'kind': 'entry', 'description': 'Optional random seed for repeatable glitches.'},
            ], 12),
        ]

        self.group_frames = {}
        for group_name, items, start_row in effect_groups:
            group = ttk.LabelFrame(frame, text=group_name, padding=8)
            group.grid(row=start_row, column=0, columnspan=3, sticky='nsew', pady=6)
            self.group_frames[group_name] = group
            self._build_group_controls(group, items)

        self._toggle_group('Curve', True)
        self._toggle_group('Mosh', True)
        self._toggle_group('Output', True)

        self.controls_content = ttk.Frame(frame)
        self.controls_content.grid(row=20, column=0, columnspan=3, sticky='nsew', pady=(8, 0))

        self.status_var = tk.StringVar(value='Ready')
        ttk.Label(self.controls_content, textvariable=self.status_var).pack(anchor='w', pady=(0, 6))
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress = ttk.Progressbar(self.controls_content, orient='horizontal', mode='determinate', variable=self.progress_var, maximum=100)
        self.progress.pack(fill='x', pady=(0, 8))

        self.vividness_frame = ttk.LabelFrame(self.controls_content, text='Vividness', padding=8)
        self.vividness_frame.pack(fill='x', pady=(0, 8))
        self.vividness_canvas = tk.Canvas(self.vividness_frame, width=320, height=90, bg='#1b1a18', highlightthickness=0)
        self.vividness_canvas.pack(fill='x')
        self._draw_vividness_preview()
        self.after(100, self._animate_vividness_preview)

        self.log = tk.Text(self.controls_content, height=6, width=90)
        self.log.pack(fill='both', expand=True, pady=(0, 8))
        self.log.insert('1.0', 'Run status will appear here.\n')
        self.log.configure(state='disabled')

        self.preview_panel = ttk.LabelFrame(self.controls_content, text='Preview export', padding=8)
        self.preview_panel.pack(fill='x', pady=(0, 8))
        ttk.Label(self.preview_panel, text='Render a short preview file instead of an in-app player.', foreground='#555').pack(anchor='w')
        ttk.Label(self.preview_panel, text='The preview is written as a .preview.mp4 next to your input video.', foreground='#777').pack(anchor='w', pady=(4, 0))

        self.action_frame = ttk.Frame(self.controls_content)
        self.action_frame.pack(fill='x')
        self.preview_button = ttk.Button(self.action_frame, text='Render 15s Preview', command=self._run_preview, width=20)
        self.preview_button.pack(side='left', padx=(0, 8))
        self.preview_button.configure(style='Accent.TButton')
        self.run_button = ttk.Button(self.action_frame, text='Run', command=self._run_pipeline, width=14)
        self.run_button.pack(side='left')
        self.run_button.configure(style='Success.TButton')

        self.controls_content.columnconfigure(0, weight=1)

    def _pick_input(self):
        path = filedialog.askopenfilename(filetypes=[('Video files', '*.mp4 *.mov *.mkv *.avi *.webm *.m4v')])
        if path:
            self.input_var.set(path)

    def _pick_output(self):
        path = filedialog.asksaveasfilename(defaultextension='.mp4', filetypes=[('MP4', '*.mp4'), ('WebM', '*.webm')])
        if path:
            self.output_var.set(path)

    def _build_group_controls(self, group, items):
        for index, item in enumerate(items):
            label = item['label']
            variable = item['variable']
            kind = item.get('kind', 'slider')

            row = ttk.Frame(group)
            row.pack(fill='x', pady=(4, 2))
            ttk.Label(row, text=label, width=24, anchor='w').pack(side='left')

            if kind == 'entry':
                ttk.Entry(row, textvariable=variable, width=18).pack(side='left', padx=(8, 6))
            else:
                scale = ttk.Scale(row, from_=item['from_'], to=item['to'], orient='horizontal')
                scale.pack(side='left', fill='x', expand=True, padx=(8, 6))
                value_var = tk.StringVar(value=self._format_slider_value(variable.get(), item['step']))
                scale.set(float(variable.get()))
                scale.configure(command=lambda value, var=variable, disp=value_var, step=item['step']: self._set_slider_value(value, var, disp, step))
                self._set_slider_value(scale.get(), variable, value_var, item['step'])
                ttk.Label(row, textvariable=value_var, width=8, anchor='e').pack(side='left')

            description = item.get('description', '')
            if description:
                ttk.Label(group, text=description, foreground='#666', wraplength=360, justify='left').pack(fill='x', padx=(24, 0), pady=(0, 4))

    def _toggle_group(self, name, expanded):
        group = self.group_frames.get(name)
        if not group:
            return
        for child in group.winfo_children():
            if expanded:
                child.pack(fill='x') if child.winfo_manager() == 'pack' else None
            else:
                child.pack_forget()

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

    def _draw_vividness_preview(self):
        self.vividness_canvas.delete('all')
        width = 320
        height = 90
        mid_y = height / 2
        cycles = float(self.cycles_var.get())
        motion_weight = float(self.motion_weight_var.get())
        audio_weight = float(self.audio_weight_var.get())
        self.vividness_canvas.create_rectangle(0, 0, width, height, fill='#1b1a18', outline='')
        self.vividness_canvas.create_line(10, mid_y, width - 10, mid_y, fill='#4f4b42', width=1)
        for i in range(0, width - 20, 8):
            t = i / max(width - 20, 1)
            base = 0.5 + 0.5 * math.sin(2 * math.pi * cycles * t)
            motion_bias = 0.5 + 0.5 * math.sin(2 * math.pi * 0.35 * (i / 10))
            audio_bias = 0.5 + 0.5 * math.sin(2 * math.pi * 0.15 * (i / 10) + 1.2)
            value = clamp((1 - motion_weight - audio_weight) * base + motion_weight * motion_bias + audio_weight * audio_bias)
            px = 10 + i
            py = mid_y - (value - 0.5) * 28
            color = '#f0c36d' if value > 0.65 else '#8b6f47'
            self.vividness_canvas.create_oval(px - 2, py - 2, px + 2, py + 2, fill=color, outline='')
        self.vividness_canvas.create_text(20, 72, anchor='nw', text='loop-safe vividness', fill='#d9d2c3', font=('Helvetica', 9))
        self.vividness_canvas.create_text(180, 72, anchor='nw', text='motion-biased', fill='#8b6f47', font=('Helvetica', 9))

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
            'preview_duration': int(float(self.preview_var.get())),
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
