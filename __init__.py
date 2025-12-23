from .buildtool import BuildConfig, INCFLAGS, main
from .buildtool import build as _build, Path, Target, build_compilation_database as _build_compilation_database
import os

def build(filename: str|list[str], cfg: BuildConfig):
    if not isinstance(filename, str):
        path = Path(filename[0])
        name = path.with_suffix('')
        target = Target(name, cfg)
        
        for fname in filename:
            path = Path(fname)
            target.compile(path)
            
        os.makedirs(cfg.BINDIR, exist_ok=True)

        target.link()
    else:
        path = Path(filename)
        _build(path, cfg)


def build_compilation_database(src_files: list[str], cfg: BuildConfig, outfile='compile_commands.json'):
    paths = [Path(p) for p in src_files]
    _build_compilation_database(Path(outfile), paths,cfg)