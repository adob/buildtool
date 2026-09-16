"""Build requested modules and library sources for PlatformIO's native application compiler."""

import json
import os
from pathlib import Path
import subprocess
import sys

import buildtool as bt
from cmake_modules import artifact_manifest, archive_objects, publish_consumer_files, write_changed


class LibraryTarget(bt.Target):
    """Resolve named modules across every registered library root."""

    def __init__(self, cfg: bt.BuildConfig, roots: list[str], module_roots: dict[str, str]) -> None:
        """Search roots, in registration order, for module interface sources."""
        super().__init__(bt.Path('platformio-modules'), cfg)
        self.search_roots = roots
        self.module_roots = module_roots

    def mod2src(self, modname: str | None, type: bt.SourceType) -> bt.Path:
        """Find named modules in the registered roots; other sources resolve normally."""
        if type == bt.SourceType.MODULE:
            for prefix in sorted(self.module_roots, key=len, reverse=True):
                if modname == prefix:
                    local_name = prefix.rsplit('.', 1)[-1]
                elif modname.startswith(prefix + '.') or modname.startswith(prefix + ':'):
                    local_name = modname[len(prefix) + 1:]
                else:
                    continue
                return super().mod2src(
                    local_name, type, search_roots=[self.module_roots[prefix]])
            return super().mod2src(modname, type, search_roots=self.search_roots)
        return super().mod2src(modname, type)


def build(manifest: Path) -> None:
    """Build modules and sources described by manifest using the application's cross-toolchain."""
    settings = json.loads(manifest.read_text())
    directory = manifest.parent
    os.chdir(settings['roots'][0])
    cfg = bt.BuildConfig(
        CC=settings['cc'], CXX=settings['cxx'], CFLAGS=settings['cflags'],
        CXXFLAGS=settings['cxxflags'], INCFLAGS=[], LDFLAGS=[],
        SRCDIR='.', OBJDIR=str(directory / 'artifacts'),
        DEPDIR=str(directory / 'artifacts/deps'), TAGS=settings['tags'],
        USE_DIRECTORY_CONFIG=False, STD_HEADER_UNIT=False,
        ABSOLUTE_MODULE_PATHS=True, JOBS=1, memory=bt.MemoryBudget())
    target = LibraryTarget(cfg, settings['roots'], settings.get('module_roots', {}))
    # Absolute source paths keep object names stable regardless of the working root.
    requests = [(target.mod2src(name, bt.SourceType.MODULE), bt.SourceType.MODULE, name, None)
                for name in settings['modules']]
    requests.extend((bt.Path(source), None, None, None) for source in settings.get('sources', []))
    target.compile_many(requests)
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
