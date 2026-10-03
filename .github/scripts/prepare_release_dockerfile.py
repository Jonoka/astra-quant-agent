"""Derive the release build recipe without modifying official source bytes."""
import hashlib
from pathlib import Path
import sys


def main(source: Path, output: Path):
    raw = (source / 'Dockerfile').read_bytes()
    anchor = b'RUN rm -rf public/images && mkdir -p public/images\n'
    assert raw.count(anchor) == 1, 'Official Dockerfile anchor changed'
    image = source / 'docs/images/dashboard_preview.png'
    assert image.is_file() and image.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'), 'Official preview missing'
    # Remove the external Git symlink first, then populate the real directory.
    recipe = raw.replace(anchor, anchor + b'COPY docs/images/dashboard_preview.png ./public/images/dashboard_preview.png\n', 1)
    assert output.parent.resolve() != source.resolve() and not output.exists()
    output.write_bytes(recipe)
    print('sha256=' + hashlib.sha256(recipe).hexdigest())


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
