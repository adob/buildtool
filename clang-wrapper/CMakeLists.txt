cmake_minimum_required(VERSION 3.20)
project(buildtool_clang LANGUAGES C CXX)

# Clang_DIR must point at the build/install of our patched Clang, not the
# system package: ModuleLoader::loadHeaderUnit is an extension of that fork.
find_package(Clang REQUIRED CONFIG)
add_executable(buildtool-clang main.cpp)
target_compile_features(buildtool-clang PRIVATE cxx_std_17)
target_include_directories(buildtool-clang SYSTEM PRIVATE
  ${CLANG_INCLUDE_DIRS} ${LLVM_INCLUDE_DIRS})
separate_arguments(LLVM_DEFINITIONS_LIST NATIVE_COMMAND ${LLVM_DEFINITIONS})
target_compile_options(buildtool-clang PRIVATE ${LLVM_DEFINITIONS_LIST})
if(NOT LLVM_ENABLE_RTTI)
  target_compile_options(buildtool-clang PRIVATE -fno-rtti)
endif()
llvm_map_components_to_libnames(WRAPPER_LLVM_LIBS native)
target_link_libraries(buildtool-clang PRIVATE
  clangFrontendTool clangDriver clangFrontend clangSerialization clangCodeGen
  ${WRAPPER_LLVM_LIBS})
install(TARGETS buildtool-clang RUNTIME DESTINATION bin)
