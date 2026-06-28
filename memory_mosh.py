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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import avimosh


def run_ffmpeg(args, label):
    cmd = ['ffmpeg', '-y', *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f'\n--- ffmpeg failed during: {label} ---', file=sys.stderr)
        print(result.stderr[-2000:], file=sys.stderr)
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description='Real I-frame-removal / delta-frame-duplication datamoshing.')
    p.add_argument('input', help='source video file')
    p.add_argument('output', help='output video file (.mp4 or .webm)')
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
    args = p.parse_args()

    if shutil.which('ffmpeg') is None:
        print('ffmpeg not found on PATH. Install it from https://ffmpeg.org/download.html', file=sys.stderr)
        sys.exit(1)

    in_path = Path(args.input)
    out_path = Path(args.output)
    if not in_path.exists():
        print(f'input file not found: {in_path}', file=sys.stderr)
        sys.exit(1)

    workdir = Path(tempfile.mkdtemp(prefix='memory-mosh-')) if not args.keep_intermediate else out_path.parent
    raw_avi = workdir / f'{in_path.stem}_raw.avi'
    moshed_avi = workdir / f'{in_path.stem}_moshed.avi'

    print(f'[1/3] transcoding to raw AVI/mpeg4 (rare keyframes)…')
    run_ffmpeg(['-i', str(in_path), '-c:v', 'mpeg4', '-g', '9999', '-bf', '0',
                '-q:v', str(args.quality), '-an', str(raw_avi)], 'transcode')

    print(f'[2/3] removing keyframes, duplicating delta frames…')
    stats = avimosh.mosh_file(
        str(raw_avi), str(moshed_avi),
        keyframe_removal_rate=args.keyframe_removal_rate,
        duplicate_rate=args.duplicate_rate,
        duplicate_range=(args.duplicate_min, args.duplicate_max),
        freeze_chance=args.freeze_chance,
        freeze_range=(args.freeze_min, args.freeze_max),
        seed=args.seed,
    )
    print(f'      {stats["keyframes_removed"]} keyframes removed, '
          f'{stats["frames_duplicated"]} duplicate frames added '
          f'({stats["original_frames"]} -> {stats["output_frames"]} frames)')

    print(f'[3/3] re-encoding to {out_path.suffix} (corruption baked into the pixels)…')
    if out_path.suffix.lower() == '.webm':
        codec_args = ['-c:v', 'libvpx-vp9', '-crf', '30', '-b:v', '0']
    else:
        codec_args = ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18']
    run_ffmpeg(['-i', str(moshed_avi), *codec_args, str(out_path)], 're-encode')

    if not args.keep_intermediate:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f'kept intermediates: {raw_avi}, {moshed_avi}')

    print(f'done -> {out_path}')


if __name__ == '__main__':
    main()
