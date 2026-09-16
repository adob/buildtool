"""Build explicitly requested modules for PlatformIO's native application compiler."""

import json
import os
from pathlib import Path
import subprocess
import sys

import buildtool as bt
from cmake_modules import artifact_manifest, archive_objects, publish_consumer_files, write_changed


def build(manifest: Path) -> None:
    """Build modules described by manifest using the application's cross-toolchain."""
    settings = json.loads(manifest.read_text())
    directory = manifest.parent
    os.chdir(settings['root'])
    cfg = bt.BuildConfig(
        CC=settings['cc'], CXX=settings['cxx'], CFLAGS=settings['cflags'],
        CXXFLAGS=settings['cxxflags'], INCFLAGS=[], LDFLAGS=[],
        SRCDIR='.', OBJDIR=str(directory / 'artifacts'),
        DEPDIR=str(directory / 'artifacts/deps'), TAGS=settings['tags'],
        USE_DIRECTORY_CONFIG=False, STD_HEADER_UNIT=False,
        ABSOLUTE_MODULE_PATHS=True, JOBS=1, memory=bt.MemoryBudget())
    target = bt.Target(bt.Path('platformio-modules'), cfg)
    target.compile_many([(target.mod2src(name, bt.SourceType.MODULE),
                          bt.SourceType.MODULE, name, None) for name in settings['modules']])
    for field in ('archiver', 'ranlib'):
        write_changed(directory / field, settings[field] + '\n')
    archive_objects(directory, target.objs, directory)
    write_changed(directory / 'native_modules', 'FALSE\n')
    publish_consumer_files(directory, artifact_manifest([cfg]))


if __name__ == '__main__':
    try:
        build(Path(sys.argv[1]).resolve())
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode if error.returncode > 0 else 128 - error.returncode) from None
    except (RuntimeError, ValueError, OSError) as error:
        if not getattr(error, 'buildtool_reported', False):
            print(f'buildtool: error: {error}', file=sys.stderr)
        raise SystemExit(1) from None
