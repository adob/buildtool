from .buildtool import BuildConfig, INCFLAGS, main
from .buildtool import build as _build, Path, Target, build_compilation_database as _build_compilation_database
from .buildtool import reset_build_state
from .vfs import FileSystem, RealFileSystem, MemoryFileSystem
from .memory import MemoryBudget

def build(filename: str | list[str], cfg: BuildConfig) -> None:
    if not isinstance(filename, str):
        path = Path(filename[0])
        name = path.with_suffix('')
        target = Target(name, cfg)
        
        target.compile_many([Path(fname) for fname in filename])
            
        target.link()
    else:
        path = Path(filename)
        _build(path, cfg)


def build_compilation_database(
    src_files: list[str],
    cfg: BuildConfig,
    outfile: str = 'compile_commands.json',
) -> None:
    paths = [Path(p) for p in src_files]
    _build_compilation_database(Path(outfile), paths,cfg)
