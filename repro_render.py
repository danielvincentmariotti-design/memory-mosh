import time
from memory_mosh import run_pipeline

cfg = {
    'input': 'D:/Memory Mosh/3MXl09TdCuIbYX7D0Ogv (1).mp4',
    'output': 'D:/Memory Mosh/verify_output.mp4',
    'use_vividness_curve': True,
    'include_audio': False,
    'analysis_fps': 4.0,
    'curve_cycles': 1.5,
    'motion_weight': 0.55,
    'audio_weight': 0.15,
    'keyframe_removal_rate': 0.9,
    'duplicate_rate': 0.15,
    'duplicate_min': 2,
    'duplicate_max': 4,
    'freeze_chance': 0.02,
    'freeze_min': 6,
    'freeze_max': 18,
    'quality': 3,
    'seed': None,
    'keep_intermediate': True,
}

start = time.time()
print('START')
try:
    result = run_pipeline(cfg, lambda msg: print('PROGRESS', msg))
    print('DONE', result.get('output_path'))
except Exception as exc:
    print('ERROR', type(exc).__name__, exc)
finally:
    print('ELAPSED', round(time.time() - start, 2))
