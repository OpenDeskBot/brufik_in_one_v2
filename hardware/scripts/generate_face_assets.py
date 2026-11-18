"""PlatformIO pre-build: fail on invalid expression source; regenerate changed assets."""
Import('env')
import runpy
from pathlib import Path
root = Path(env['PROJECT_DIR'])
module = runpy.run_path(str(root / 'tools/face_assets.py'))
bundle = module['generate']()
print(f'==> Face assets: {len(bundle["assets"])} expressions, {len(bundle["phonemes"])} phonemes, version={bundle["version"]}')
