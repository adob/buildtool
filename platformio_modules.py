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

    def __init__(self, cfg: bt.BuildConfig, roots: list[str], module_roots: dict[str, str],
                 generation_configs: dict[str, bt.BuildConfig] | None = None) -> None:
        """Search roots, in registration order, for module interface sources."""
        super().__init__(bt.Path('platformio-modules'), cfg)
        self.search_roots = roots
        self.module_roots = module_roots
        self.generation_configs = generation_configs or {}

    def mapped_module(self, modname: str) -> tuple[str, str] | None:
        """Return (prefix, local name) for a module rooted in a registered library."""
        for prefix in sorted(self.module_roots, key=len, reverse=True):
            if modname == prefix:
                return prefix, prefix.rsplit('.', 1)[-1]
            if modname.startswith(prefix + '.') or modname.startswith(prefix + ':'):
                return prefix, modname[len(prefix) + 1:]
        return None

    def mod2src(self, modname: str | None, type: bt.SourceType) -> bt.Path:
        """Find named modules in the registered roots; other sources resolve normally."""
        if type == bt.SourceType.MODULE:
            mapped = self.mapped_module(modname)
            if mapped is not None:
                prefix, local_name = mapped
                return super().mod2src(
                    local_name, type, search_roots=[self.module_roots[prefix]])
            return super().mod2src(modname, type, search_roots=self.search_roots)
        return super().mod2src(modname, type)

    async def resolve_module_source(self, name: str, type: bt.SourceType,
                                    directory: bt.DirectoryConfig | None,
                                    parent: bt.Job) -> bt.Path:
        """Resolve mapped modules, materializing their library-local GENERATED fallback."""
        mapped = self.mapped_module(name) if type == bt.SourceType.MODULE else None
        if mapped is None:
            return await super().resolve_module_source(name, type, directory, parent)
        try:
            return self.mod2src(name, type)
        except RuntimeError as error:
            prefix, local_name = mapped
            generated_cfg = self.generation_configs.get(prefix)
            if generated_cfg is None:
                raise
            generated_target = bt.Target(bt.Path('platformio-generated-' + prefix), generated_cfg)
            generated_target.session = self.session
            generated_target.job_sources = self.job_sources
            generated_target.link_events = self.link_events
            generated = await generated_target.resolve_generated_module_source(local_name, parent)
            if generated is None:
                raise error
            logical = generated_cfg.generated_logical_paths.get(generated, generated)
            relative = bt.source_relative_path(logical, generated_cfg) or logical
            self.cfg.generated_logical_paths[generated] = (
                bt.Path('__generated__') / prefix.replace('.', '/') / relative)
            return generated


def build(manifest: Path) -> None:
    """Build modules and sources described by manifest using the application's cross-toolchain."""
    settings = json.loads(manifest.read_text())
    directory = manifest.parent
    roots = [Path(value).resolve() for value in settings['roots']]
    common_root = Path(os.path.commonpath([str(root) for root in roots]))
    os.chdir(common_root)
    cfg = bt.BuildConfig(
        CC=settings['cc'], CXX=settings['cxx'], CFLAGS=settings['cflags'],
        CXXFLAGS=settings['cxxflags'], INCFLAGS=[], LDFLAGS=[],
        SRCDIR='.', OBJDIR=str(directory / 'artifacts'),
        DEPDIR=str(directory / 'artifacts/deps'), TAGS=settings['tags'],
        USE_DIRECTORY_CONFIG=False, STD_HEADER_UNIT=False,
        ABSOLUTE_MODULE_PATHS=True, JOBS=1, memory=bt.MemoryBudget())

    module_roots = settings.get('module_roots', {})
    host_incflags = [
        '-I' + os.path.relpath(root, common_root)
        for root in roots
        if root != common_root
    ]
    generation_configs: dict[str, bt.BuildConfig] = {}
    for prefix, root_value in module_roots.items():
        root = Path(root_value).resolve()
        relative_root = os.path.relpath(root, common_root)
        key = prefix.replace('.', '_').replace(':', '_')
        host_cxx = settings.get('host_cxx') or ''
        host_cc = settings.get('host_cc') or host_cxx
        if not host_cxx:
            raise RuntimeError('A native host C++ compiler is required for generated build tools')
        host_cfg = bt.BuildConfig(
            CC=host_cc, CXX=host_cxx, SRCDIR='.',
            OBJDIR=str(directory / 'host' / key),
            DEPDIR=str(directory / 'host' / key / 'deps'),
            INCFLAGS=host_incflags, JOBS=1)
        generation_configs[prefix] = bt.BuildConfig(
            CC=settings['cc'], CXX=settings['cxx'], CFLAGS=settings['cflags'],
            CXXFLAGS=settings['cxxflags'], INCFLAGS=[], LDFLAGS=[],
            SRCDIR=relative_root, OBJDIR=str(directory / 'generated' / key),
            DEPDIR=str(directory / 'generated' / key / 'deps'), TAGS=settings['tags'],
            STD_HEADER_UNIT=False, ABSOLUTE_MODULE_PATHS=True, JOBS=1,
            EXEC_CONFIG=host_cfg)

    target = LibraryTarget(cfg, settings['roots'], module_roots, generation_configs)
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
