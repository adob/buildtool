// Copyright (c) 2026 buildtool contributors. See ../LICENSE.
// Prototype: extract one module interface, then validate it as an ordinary header.
#include "clang/AST/ASTConsumer.h"
#include "clang/AST/ASTContext.h"
#include "clang/AST/DeclCXX.h"
#include "clang/AST/DeclTemplate.h"
#include "clang/AST/PrettyPrinter.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/FrontendActions.h"
#include "clang/Lex/Lexer.h"
#include "clang/Lex/PPCallbacks.h"
#include "clang/Lex/Preprocessor.h"
#include "clang/Tooling/CommonOptionsParser.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/InitLLVM.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/raw_ostream.h"
#include <memory>
#include <string>
#include <vector>

using namespace clang;

namespace {
llvm::cl::OptionCategory Category("module-to-header options");
llvm::cl::opt<std::string> Output("o", llvm::cl::Required,
    llvm::cl::desc("Generated header path"), llvm::cl::cat(Category));

// Replace any -x setting with Language before the input, where the driver applies it.
tooling::ArgumentsAdjuster languageArguments(std::string Language) {
  return [Language](const tooling::CommandLineArguments &Args, llvm::StringRef) {
    tooling::CommandLineArguments Adjusted{Args.front(), "-x", Language};
    for (size_t I = 1; I < Args.size(); ++I) {
      if (Args[I] == "-x") {
        ++I;
        continue;
      }
      if (!llvm::StringRef(Args[I]).starts_with("-x"))
        Adjusted.push_back(Args[I]);
    }
    return Adjusted;
  };
}

struct Header {
  std::vector<std::string> Includes;
  std::string Declarations;
  bool Failed = false;
  bool HasModuleAttachment = false;
};

// Report an unsupported construct at Loc; Text explains why extraction stops.
void reject(CompilerInstance &CI, Header &Result, SourceLocation Loc,
            llvm::StringRef Text) {
  Result.Failed = true;
  unsigned ID = CI.getDiagnostics().getCustomDiagID(
      DiagnosticsEngine::Error, "module-to-header: %0");
  CI.getDiagnostics().Report(Loc, ID) << Text;
}

class Includes final : public PPCallbacks {
  CompilerInstance &CI;
  Header &Result;

public:
  // Collect direct includes from CI into Result, excluding transitive headers.
  Includes(CompilerInstance &CI, Header &Result) : CI(CI), Result(Result) {}

  // Preserve the spelling of an active include whose directive starts at Loc.
  void InclusionDirective(SourceLocation Loc, const Token &, llvm::StringRef Name,
                          bool Angled, CharSourceRange, OptionalFileEntryRef,
                          llvm::StringRef, llvm::StringRef, const Module *, bool,
                          SrcMgr::CharacteristicKind) override {
    if (CI.getSourceManager().isWrittenInMainFile(Loc))
      Result.Includes.push_back("#include " + std::string(Angled ? "<" : "\"") +
                                Name.str() + (Angled ? ">\n" : "\"\n"));
  }

  // Local macro changes cannot be safely relocated before all exported declarations.
  void MacroDefined(const Token &Name, const MacroDirective *) override {
    if (CI.getSourceManager().isWrittenInMainFile(Name.getLocation()))
      reject(CI, Result, Name.getLocation(),
             "local #define is unsupported; move shared macros to an included header");
  }

  // Undefining Name also changes the preprocessing environment of retained bodies.
  void MacroUndefined(const Token &Name, const MacroDefinition &,
                      const MacroDirective *) override {
    if (CI.getSourceManager().isWrittenInMainFile(Name.getLocation()))
      reject(CI, Result, Name.getLocation(), "local #undef is unsupported");
  }

  // Pragmas at Loc can affect ABI or code generation and need explicit handling.
  void PragmaDirective(SourceLocation Loc, PragmaIntroducerKind) override {
    if (CI.getSourceManager().isWrittenInMainFile(Loc))
      reject(CI, Result, Loc, "source-local pragmas are unsupported");
  }
};

class Extractor final : public ASTConsumer {
  CompilerInstance &CI;
  Header &Result;

  // Copy D's spelling; preserve templates, constraints, attributes, and bodies.
  std::string source(const Decl *D) {
    auto Range = D->getSourceRange();
    if (Range.getBegin().isMacroID() || Range.getEnd().isMacroID()) {
      reject(CI, Result, D->getLocation(), "macro-generated declarations are unsupported");
      return {};
    }
    bool Invalid = false;
    auto Text = Lexer::getSourceText(CharSourceRange::getTokenRange(Range),
        CI.getSourceManager(), CI.getLangOpts(), &Invalid);
    if (Invalid)
      reject(CI, Result, D->getLocation(), "declaration has no contiguous source range");
    return Text.str();
  }

  // Emit D only when exported, passing Exported through namespace/linkage wrappers.
  std::string declaration(const Decl *D, bool Exported = false) {
    if (D->isImplicit() || !CI.getSourceManager().isWrittenInMainFile(
                               CI.getSourceManager().getExpansionLoc(D->getLocation())))
      return {};
    if (isa<ImportDecl>(D)) {
      reject(CI, Result, D->getLocation(),
             "imports need a module-to-header mapping; this prototype handles one interface");
      return {};
    }
    if (auto *E = dyn_cast<ExportDecl>(D))
      return declarations(E, true);
    if (auto *N = dyn_cast<NamespaceDecl>(D)) {
      auto Body = declarations(N, Exported);
      if (Body.empty())
        return {};
      return std::string(N->isInline() ? "inline " : "") + "namespace " +
             N->getNameAsString() + " {\n" + Body + "}\n";
    }
    if (auto *L = dyn_cast<LinkageSpecDecl>(D)) {
      auto Body = declarations(L, Exported);
      if (Body.empty())
        return {};
      return std::string("extern \"") +
             (L->getLanguage() == LinkageSpecLanguageIDs::C ? "C" : "C++") +
             "\" {\n" + Body + "}\n";
    }
    if (!Exported && !D->isInExportDeclContext())
      return {};
    Result.HasModuleAttachment |= D->isInNamedModule();

    if (auto *F = dyn_cast<FunctionDecl>(D)) {
      if (!F->isInlined() && !F->isConstexpr()) {
        if (F->getReturnType()->getContainedAutoType()) {
          reject(CI, Result, D->getLocation(),
                 "a non-inline function with a deduced return type needs explicit inline "
                 "or an explicit return type for header extraction");
          return {};
        }
        // Clang's terse printer emits a signature without its implementation.
        PrintingPolicy Policy(CI.getLangOpts());
        Policy.TerseOutput = true;
        std::string Text;
        llvm::raw_string_ostream Stream(Text);
        F->print(Stream, Policy);
        return Text + ";\n";
      }
      return source(D) + ";\n";
    }
    if (auto *V = dyn_cast<VarDecl>(D)) {
      if (!V->isInline() && !V->isConstexpr()) {
        PrintingPolicy Policy(CI.getLangOpts());
        Policy.SuppressInitializers = true;
        std::string Text;
        llvm::raw_string_ostream Stream(Text);
        V->print(Stream, Policy);
        return std::string(V->getStorageClass() == SC_Extern ? "" : "extern ") +
               Text + ";\n";
      }
    }
    return source(D) + ";\n";
  }

  // Emit the lexical declarations in DC with the inherited export state.
  std::string declarations(const DeclContext *DC, bool Exported) {
    std::string Text;
    for (const Decl *D : DC->decls())
      Text += declaration(D, Exported);
    return Text;
  }

public:
  // Use CI's parsed AST and diagnostics to populate Result.
  Extractor(CompilerInstance &CI, Header &Result) : CI(CI), Result(Result) {}

  // Extract only after Context is fully parsed and free of compiler errors.
  void HandleTranslationUnit(ASTContext &Context) override {
    if (CI.getDiagnostics().hasErrorOccurred()) {
      Result.Failed = true;
      return;
    }
    if (!Context.getCurrentNamedModule()) {
      reject(CI, Result, {}, "input must be a named module interface");
      return;
    }
    Result.Declarations = declarations(Context.getTranslationUnitDecl(), false);
  }
};

class ExtractAction final : public ASTFrontendAction {
  Header &Result;

public:
  // Keep extraction results after the frontend and its AST have been destroyed.
  explicit ExtractAction(Header &Result) : Result(Result) {}

  // Install preprocessing callbacks and the consumer for CI's input file.
  std::unique_ptr<ASTConsumer> CreateASTConsumer(CompilerInstance &CI,
                                                llvm::StringRef) override {
    CI.getPreprocessor().addPPCallbacks(std::make_unique<Includes>(CI, Result));
    return std::make_unique<Extractor>(CI, Result);
  }
};

class Factory final : public tooling::FrontendActionFactory {
  Header &Result;

public:
  // Bind each frontend action to Result for this single-input invocation.
  explicit Factory(Header &Result) : Result(Result) {}
  // Create the single-input extractor requested by ClangTool.
  std::unique_ptr<FrontendAction> create() override {
    return std::make_unique<ExtractAction>(Result);
  }
};
} // namespace

// Parse Argv, extract one module, validate the header, then atomically publish it.
int main(int Argc, const char **Argv) {
  llvm::InitLLVM Init(Argc, Argv);
  auto Options = tooling::CommonOptionsParser::create(Argc, Argv, Category);
  if (!Options) {
    llvm::errs() << Options.takeError();
    return 2;
  }
  auto Sources = Options->getSourcePathList();
  if (Sources.size() != 1) {
    llvm::errs() << "module-to-header: expected exactly one module interface\n";
    return 2;
  }
  llvm::SmallString<256> InputPath(Sources.front()), OutputPath(Output.getValue());
  llvm::sys::fs::make_absolute(InputPath);
  llvm::sys::fs::make_absolute(OutputPath);
  llvm::sys::path::remove_dots(InputPath, true);
  llvm::sys::path::remove_dots(OutputPath, true);
  if (InputPath == OutputPath || llvm::sys::fs::equivalent(InputPath, OutputPath)) {
    llvm::errs() << "module-to-header: output must not overwrite the module source\n";
    return 2;
  }
  Header Result;
  tooling::ClangTool Tool(Options->getCompilations(), Sources);
  Tool.appendArgumentsAdjuster(languageArguments("c++-module"));
  Factory Actions(Result);
  if (Tool.run(&Actions) || Result.Failed)
    return 1;
  std::string Text = "// Generated by module-to-header; do not edit.\n#pragma once\n\n";
  for (const auto &Include : Result.Includes)
    Text += Include;
  Text += "\n" + Result.Declarations;

  // Reparse as ordinary C++ at the original path, retaining its quoted-include context.
  tooling::ClangTool Verify(Options->getCompilations(), Sources);
  Verify.appendArgumentsAdjuster(languageArguments("c++-header"));
  Verify.mapVirtualFile(InputPath, Text);
  if (Verify.run(tooling::newFrontendActionFactory<SyntaxOnlyAction>().get())) {
    llvm::errs() << "module-to-header: generated header validation failed; output unchanged\n";
    return 1;
  }
  if (Result.HasModuleAttachment)
    llvm::errs() << "module-to-header: note: exported declarations are attached to a named module; "
                    "the generated header does not fix their ABI. Use extern \"C++\" in the module "
                    "for compatibility with header consumers.\n";

  llvm::SmallString<256> Temporary;
  int FD;
  auto Error = llvm::sys::fs::createUniqueFile(Output.getValue() + ".tmp-%%%%%%", FD, Temporary);
  if (!Error) {
    llvm::raw_fd_ostream Stream(FD, true);
    Stream << Text;
    Stream.close();
    Error = Stream.error();
    Stream.clear_error();
    if (!Error)
      Error = llvm::sys::fs::rename(Temporary, OutputPath);
    if (Error)
      llvm::sys::fs::remove(Temporary);
  }
  if (Error) {
    llvm::errs() << "module-to-header: " << Error.message() << "\n";
    return 1;
  }
  return 0;
}
