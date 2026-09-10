"""Bootstrap with: bt build deps/buildtool/clang-wrapper/main.cpp.

These defaults match the local patched LLVM checkout. Override the environment
variables for another checkout/install, then remove this directory's cached
buildvars.json so buildtool reevaluates the configuration.
"""

import os
from pathlib import Path
import shlex
import subprocess

clang_build = Path(os.environ.get(
    "BT_CLANG_BUILD", Path.home() / "llvm-project/build-header-imports"))
clang_source = Path(os.environ.get(
    "BT_CLANG_SOURCE", clang_build.parent / "clang"))
llvm_config = os.environ.get(
    "BT_LLVM_CONFIG", str(Path.home() / "Downloads/LLVM-23.1.0-Linux-X64/bin/llvm-config"))


def llvm_flags(*args: str) -> list[str]:
    """Return LLVM flags from llvm-config invoked with args."""
    return shlex.split(subprocess.check_output([llvm_config, *args], text=True))


# Put patched Clang headers before LLVM's include directory, which may contain
# stock Clang headers too. -isystem also avoids buildtool rewriting -I flags.
CFLAGS = ["-isystem", str(clang_source / "include"),
          "-isystem", str(clang_build / "include"),
          *llvm_flags("--cxxflags"), "-O2"]
if llvm_flags("--assertion-mode") == ["OFF"]:
    CFLAGS.append("-DNDEBUG")

# These are the Clang libraries used by ExecuteCompilerInvocation in our
# patched Clang 23 build. Group the static archives so their mutual references
# do not depend on link ordering. LLVM selects its own component dependencies.
clang_libraries = """
FrontendTool Driver Frontend Serialization DependencyScanning
ScalableStaticAnalysisFrontend ScalableStaticAnalysisAnalyses
ScalableStaticAnalysisSourceTransformation ScalableStaticAnalysisCore
ToolingCore CodeGen ExtractAPI UnifiedSymbolResolution InstallAPI
RewriteFrontend Options Parse Sema APINotes AnalysisLifetimeSafety Analysis
ASTMatchers Support Edit AST Rewrite Lex Basic
""".split()

llvm_components = """
native coverage frontenddriver lto extensions passes hipstdpar
textapibinaryreader plugins frontendopenmp frontendoffloading objectyaml
frontendatomic frontenddirective option abi windowsdriver
""".split()
system_libraries = (shlex.split(os.environ["BT_LLVM_SYSTEM_LIBS"])
                    if "BT_LLVM_SYSTEM_LIBS" in os.environ else
                    llvm_flags("--link-static", "--system-libs", *llvm_components))

LDFLAGS = [f"-L{clang_build / 'lib'}", *llvm_flags("--ldflags"),
           "-Wl,--start-group",
           *(f"-lclang{name}" for name in clang_libraries),
           *llvm_flags("--link-static", "--libs", *llvm_components),
           "-Wl,--end-group", *system_libraries]
