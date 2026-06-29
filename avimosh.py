"""
avimosh.py — real datamoshing at the AVI container/bitstream level.

This does the actual thing, not a simulation: it removes I-frame (keyframe)
chunks from the encoded stream so the decoder is forced to keep applying
later P-frame (delta-frame) motion vectors to a stale reference image, and
it duplicates selected delta-frame chunks so motion vectors get applied
repeatedly (dragging content further each repeat).

Why AVI + mpeg4/xvid: this container's frame index ('idx1') makes it
straightforward to tell I-frames from delta frames and to splice the raw
chunks directly, and ffmpeg's own mpeg4 decoder is permissive about
missing keyframes (it'll render the corruption rather than hard-erroring),
which is exactly why classic desktop datamosh tools use this combination.

No third-party dependencies — just ffmpeg (called via subprocess) and the
standard library for the binary surgery.
"""

import struct
import random


AVIIF_KEYFRAME = 0x10


def rate_from_curve(base_rate, vividness, floor=0.02, strength=0.6):
    """Scale a base rate down as vividness rises, keeping a minimum floor."""
    vibe = max(0.0, min(1.0, vividness))
    return max(floor, base_rate * (1.0 - vibe * strength))


def read_chunks(data, start, end):
    """Yield (tag, data_offset, size) for RIFF chunks in data[start:end]."""
    pos = start
    while pos + 8 <= end:
        tag = data[pos:pos + 4]
        size = struct.unpack('<I', data[pos + 4:pos + 8])[0]
        data_offset = pos + 8
        yield tag, data_offset, size
        pos = data_offset + size + (size % 2)


def parse_avi(data):
    """Locate the header, movi, and idx1 sections of an AVI file."""
    if data[0:4] != b'RIFF' or data[8:12] != b'AVI ':
        raise ValueError('not a RIFF/AVI file')

    avih = None        # (data_offset, size)
    strh_list = []      # [(data_offset, size), ...]
    movi = None         # (list_tag_offset, list_size) — offset points at the 'movi' fourcc
    idx1 = None         # (data_offset, size)

    for tag, off, size in read_chunks(data, 12, len(data)):
        if tag == b'LIST':
            list_type = data[off:off + 4]
            if list_type == b'hdrl':
                for itag, ioff, isize in read_chunks(data, off + 4, off + size):
                    if itag == b'avih':
                        avih = (ioff, isize)
                    elif itag == b'LIST' and data[ioff:ioff + 4] == b'strl':
                        for jtag, joff, jsize in read_chunks(data, ioff + 4, ioff + isize):
                            if jtag == b'strh':
                                strh_list.append((joff, jsize))
            elif list_type == b'movi':
                movi = (off, size)
        elif tag == b'idx1':
            idx1 = (off, size)

    if movi is None or idx1 is None:
        raise ValueError('AVI is missing movi or idx1 — re-export with ffmpeg defaults')
    return avih, strh_list, movi, idx1


def parse_movi_frames(data, movi):
    off, size = movi
    movi_data_start = off + 4  # past the 'movi' fourcc
    movi_data_end = off + size
    frames = []
    for tag, doff, dsize in read_chunks(data, movi_data_start, movi_data_end):
        frames.append({'tag': tag, 'offset': doff, 'size': dsize})
    return frames


def parse_idx1(data, idx1):
    off, size = idx1
    entries = []
    n = size // 16
    for i in range(n):
        base = off + i * 16
        flags = struct.unpack('<I', data[base + 4:base + 8])[0]
        entries.append(bool(flags & AVIIF_KEYFRAME))
    return entries


def annotate_frames(frames, keyframe_flags):
    if len(frames) != len(keyframe_flags):
        raise ValueError(
            f'frame count mismatch: {len(frames)} movi chunks vs {len(keyframe_flags)} idx1 entries — '
            'the file may use an index format this tool does not parse'
        )
    for f, is_key in zip(frames, keyframe_flags):
        f['is_key'] = is_key
    return frames


def mosh(frames, keyframe_removal_rate=0.9, duplicate_rate=0.15,
         duplicate_range=(2, 4), freeze_chance=0.02, freeze_range=(6, 18),
         seed=None, vividness_curve=None):
    """
    Build the moshed frame sequence.

    - The very first frame is always kept (the decoder needs one real
      keyframe to start from at all).
    - Every later keyframe is dropped with probability `keyframe_removal_rate`
      — this is the actual "I-frame removal" mechanism.
    - Delta frames are occasionally duplicated 2-4x (`duplicate_rate`) to
      drag motion further, or much longer (`freeze_chance`,
      `freeze_range`) for a held, melting freeze.
    """
    rng = random.Random(seed)
    out = []
    removed_keyframes = 0
    duplicated = 0

    for i, f in enumerate(frames):
        if i == 0:
            out.append(f)
            continue

        if f['is_key']:
            if rng.random() < keyframe_removal_rate:
                removed_keyframes += 1
                continue  # drop it — this is the whole point
            out.append(f)
            continue

        # delta frame
        frame_vividness = 0.5
        if vividness_curve:
            curve_idx = min(i - 1, len(vividness_curve) - 1)
            frame_vividness = vividness_curve[curve_idx]

        keyframe_rate = rate_from_curve(keyframe_removal_rate, frame_vividness)
        duplicate_rate_eff = rate_from_curve(duplicate_rate, frame_vividness)
        freeze_rate_eff = rate_from_curve(freeze_chance, frame_vividness)

        if rng.random() < freeze_rate_eff:
            n = rng.randint(*freeze_range)
            out.extend([f] * n)
            duplicated += n - 1
        elif rng.random() < duplicate_rate_eff:
            n = rng.randint(*duplicate_range)
            out.extend([f] * n)
            duplicated += n - 1
        else:
            out.append(f)

    stats = {
        'original_frames': len(frames),
        'output_frames': len(out),
        'keyframes_removed': removed_keyframes,
        'frames_duplicated': duplicated,
    }
    return out, stats


def rebuild_avi(data, avih, strh_list, new_frames):
    """Rebuild a full AVI file from a (possibly moshed) frame list."""
    data = bytearray(data)

    # --- rebuild the movi LIST ---
    chunk_bytes = []
    for f in new_frames:
        chunk_data = bytes(data[f['offset']:f['offset'] + f['size']])
        chunk = f['tag'] + struct.pack('<I', f['size']) + chunk_data
        if f['size'] % 2 == 1:
            chunk += b'\x00'
        chunk_bytes.append(chunk)
    movi_inner = b'movi' + b''.join(chunk_bytes)
    new_movi = b'LIST' + struct.pack('<I', len(movi_inner)) + movi_inner

    # --- rebuild idx1 (offsets are relative to the 'movi' fourcc, per the
    #     legacy AVI convention — confirmed against ffmpeg's own output) ---
    idx1_entries = bytearray()
    running_offset = 4
    for f in new_frames:
        size = f['size']
        flags = AVIIF_KEYFRAME if f.get('is_key') else 0
        idx1_entries += f['tag']
        idx1_entries += struct.pack('<I', flags)
        idx1_entries += struct.pack('<I', running_offset)
        idx1_entries += struct.pack('<I', size)
        running_offset += 8 + size + (size % 2)
    new_idx1 = b'idx1' + struct.pack('<I', len(idx1_entries)) + bytes(idx1_entries)

    # --- patch frame counts in avih / strh so headers match reality ---
    if avih is not None:
        off, _ = avih
        struct.pack_into('<I', data, off + 16, len(new_frames))  # dwTotalFrames
    for off, _ in strh_list:
        struct.pack_into('<I', data, off + 32, len(new_frames))  # dwLength

    # --- splice: everything before movi, then new movi, then new idx1 ---
    # (re-parse on the patched header bytes to find movi/idx1 boundaries)
    _, _, movi, idx1 = parse_avi(bytes(data))
    movi_start = movi[0] - 8  # back up to include the 'LIST' tag + size field
    before = bytes(data[:movi_start])
    after_idx1_start = idx1[0] - 8 + idx1[1] + (idx1[1] % 2)
    # anything after idx1 (rare, but preserve it if present)
    trailing = bytes(data[after_idx1_start:]) if after_idx1_start < len(data) else b''

    body = before + new_movi + new_idx1 + trailing
    riff_size = len(body) - 8
    out = bytearray(body)
    struct.pack_into('<I', out, 4, riff_size)
    return bytes(out)


def mosh_file(input_path, output_path, **mosh_kwargs):
    """Convenience wrapper: read, mosh, write. Returns the stats dict."""
    data = open(input_path, 'rb').read()
    avih, strh_list, movi, idx1 = parse_avi(data)
    frames = parse_movi_frames(data, movi)
    keyframe_flags = parse_idx1(data, idx1)
    frames = annotate_frames(frames, keyframe_flags)

    new_frames, stats = mosh(frames, **mosh_kwargs)
    new_data = rebuild_avi(data, avih, strh_list, new_frames)

    with open(output_path, 'wb') as fh:
        fh.write(new_data)
    return stats
