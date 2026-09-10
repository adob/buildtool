// Copyright (c) 2026 buildtool contributors. See ../LICENSE.
// A compile-only driver for the patched Clang header-unit loader.
#include "clang/Basic/Diagnostic.h"
#include "clang/Basic/DiagnosticOptions.h"
#include "clang/Basic/FileManager.h"
#include "clang/Driver/Compilation.h"
#include "clang/Driver/Driver.h"
#include "clang/Driver/Job.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/CompilerInvocation.h"
#include "clang/Frontend/TextDiagnosticPrinter.h"
#include "clang/FrontendTool/Utils.h"
#include "clang/Lex/HeaderSearch.h"
#include "clang/Lex/Preprocessor.h"
#include "clang/Lex/PreprocessorOptions.h"
#include "llvm/ADT/StringSet.h"
#include "llvm/Support/InitLLVM.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/VirtualFileSystem.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/TargetParser/Host.h"
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <optional>
#include <string>
#include <unistd.h>

using namespace clang;

namespace {
// Dedicated descriptors keep diagnostics, compiler stdout and nested compiler
// requests separate. JSON lines preserve spaces, quotes and newlines in paths.
class Mapper {
  FILE *Input;
  FILE *Output;

public:
  Mapper(FILE *Input, FILE *Output) : Input(Input), Output(Output) {}
  std::optional<std::string> request(llvm::json::Object Request) {
    std::string Line;
    llvm::raw_string_ostream OS(Line);
    OS << llvm::json::Value(std::move(Request)) << '\n';
    if (fwrite(Line.data(), 1, Line.size(), Output) != Line.size() ||
        fflush(Output) != 0) {
      llvm::errs() << "buildtool-clang: cannot write mapper request\n";
      return std::nullopt;
    }
    char *Buffer = nullptr;
    size_t Capacity = 0;
    ssize_t Size = getline(&Buffer, &Capacity, Input);
    if (Size < 0) {
      free(Buffer);
      llvm::errs() << "buildtool-clang: mapper closed before replying\n";
      return std::nullopt;
    }
    auto Reply = llvm::json::parse(llvm::StringRef(Buffer, Size));
    free(Buffer);
    if (!Reply) {
      llvm::errs() << "buildtool-clang: invalid mapper response: "
                   << llvm::toString(Reply.takeError()) << '\n';
      return std::nullopt;
    }
    if (auto *Object = Reply->getAsObject()) {
      if (auto Error = Object->getString("error"))
        llvm::errs() << "buildtool-clang: " << *Error << '\n';
      else if (auto PCM = Object->getString("pcm"); PCM && !PCM->empty())
        return PCM->str();
    }
    llvm::errs() << "buildtool-clang: mapper did not supply a PCM\n";
    return std::nullopt;
  }
};

class MappedCompiler : public CompilerInstance {
  Mapper &Modules;
  llvm::StringSet<> RequestedModules;

public:
  MappedCompiler(std::shared_ptr<CompilerInvocation> Invocation,
                 Mapper &Modules)
      : CompilerInstance(std::move(Invocation)), Modules(Modules) {}

  bool loadHeaderUnit(FileEntryRef Header, SourceLocation ImportLoc) override {
    llvm::SmallString<256> Path(Header.getName());
    getFileManager().makeAbsolutePath(Path);
    llvm::sys::path::remove_dots(Path, true);
    auto PCM = Modules.request(llvm::json::Object{
        {"kind", "header"},
        {"path", Path.str()},
        {"system", getPreprocessor().getHeaderSearchInfo().getFileDirFlavor(
                       Header) != SrcMgr::C_User}});
    if (!PCM) {
      HadFatalFailure = true;
      return false;
    }
    serialization::ModuleFile *Loaded = nullptr;
    bool Success =
        loadModuleFile(ModuleFileName::makeExplicit(*PCM), Loaded) && Loaded;
    if (!Success)
      HadFatalFailure = true;
    return Success;
  }

  ModuleLoadResult loadModule(SourceLocation ImportLoc, ModuleIdPath Path,
                              Module::NameVisibilityKind Visibility,
                              bool IsInclusionDirective) override {
    // Header units reach the loader via loadHeaderUnit above. Named modules
    // can be requested here before the ordinary loader tries their PCM path.
    std::string Name = ModuleLoader::getFlatNameFromPath(Path);
    if (!IsInclusionDirective && !Name.empty() &&
        Name != getLangOpts().CurrentModule &&
        !getPreprocessor().getHeaderSearchInfo().lookupModule(
            Name, ImportLoc, /*AllowSearch=*/false) &&
        RequestedModules.insert(Name).second) {
      auto PCM = Modules.request(
          llvm::json::Object{{"kind", "module"}, {"name", Name}});
      if (!PCM) {
        HadFatalFailure = true;
        return {};
      }
      getHeaderSearchOpts().PrebuiltModuleFiles[Name] = *PCM;
    }
    return CompilerInstance::loadModule(ImportLoc, Path, Visibility,
                                        IsInclusionDirective);
  }
};
} // namespace

int main(int Argc, char **Argv) {
  llvm::InitLLVM Init(Argc, Argv);
  // Report a disconnected buildtool as a failed compilation, not SIGPIPE.
  std::signal(SIGPIPE, SIG_IGN);
  if (Argc < 7 || llvm::StringRef(Argv[1]) != "--mapper-fds" ||
      llvm::StringRef(Argv[4]) != "--") {
    llvm::errs() << "usage: buildtool-clang --mapper-fds READ WRITE -- "
                    "/path/to/patched/clang++ <compile arguments>\n";
    return 2;
  }
  int ReadFD, WriteFD;
  if (llvm::StringRef(Argv[2]).getAsInteger(10, ReadFD) || ReadFD < 0 ||
      llvm::StringRef(Argv[3]).getAsInteger(10, WriteFD) || WriteFD < 0 ||
      ReadFD == WriteFD) {
    llvm::errs() << "buildtool-clang: invalid mapper descriptors\n";
    return 2;
  }
  FILE *Input = fdopen(ReadFD, "r"), *Output = fdopen(WriteFD, "w");
  if (!Input || !Output) {
    llvm::errs() << "buildtool-clang: cannot open mapper descriptors\n";
    return 2;
  }
  Mapper Modules(Input, Output);
  llvm::InitializeNativeTarget();
  llvm::InitializeNativeTargetAsmPrinter();
  llvm::InitializeNativeTargetAsmParser();

  auto DriverArgs = llvm::ArrayRef<const char *>(Argv + 5, Argc - 5);
  // The driver forwards color settings from its diagnostic options to cc1.
  // Parse argv as the regular Clang driver does instead of using defaults.
  auto DiagOpts = CreateAndPopulateDiagOpts(DriverArgs);
  DiagnosticsEngine Diags(DiagnosticIDs::create(), *DiagOpts,
                          new TextDiagnosticPrinter(llvm::errs(), *DiagOpts));
  // Use the real driver's path for builtin headers and toolchain discovery.
  driver::Driver Driver(Argv[5], llvm::sys::getDefaultTargetTriple(), Diags);
  std::unique_ptr<driver::Compilation> Jobs(Driver.BuildCompilation(DriverArgs));
  if (!Jobs || Diags.hasErrorOccurred())
    return 1;
  // Validate before executing anything; links/offloading/multiple inputs need
  // a full driver executor and are deliberately outside this wrapper's API.
  if (Jobs->getJobs().size() != 1 ||
      Jobs->getJobs().begin()->getArguments().empty() ||
      llvm::StringRef(Jobs->getJobs().begin()->getArguments()[0]) != "-cc1") {
    llvm::errs()
        << "buildtool-clang: expected one Clang frontend compile job\n";
    return 2;
  }
  auto Args =
      llvm::ArrayRef(Jobs->getJobs().begin()->getArguments()).drop_front();
  auto Invocation = std::make_shared<CompilerInvocation>();
  if (!CompilerInvocation::CreateFromArgs(*Invocation, Args, Diags, Argv[5]))
    return 1;
  Invocation->getPreprocessorOpts().ImplicitHeaderUnits = true;
  MappedCompiler Compiler(std::move(Invocation), Modules);
  Compiler.createVirtualFileSystem(llvm::vfs::getRealFileSystem());
  Compiler.createDiagnostics();
  bool Success = ExecuteCompilerInvocation(&Compiler);
  fclose(Input);
  fclose(Output);
  return Success && !Compiler.hadModuleLoaderFatalFailure() ? 0 : 1;
}
