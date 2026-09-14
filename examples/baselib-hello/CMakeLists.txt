cmake_minimum_required(VERSION 3.30)
project(baselib_hello LANGUAGES CXX)

include("${CMAKE_CURRENT_LIST_DIR}/../../cmake/Buildtool.cmake")

# Default to a baselib checkout next to buildtool; allow another checkout via -D.
set(BASELIB_SOURCE_ROOT "${CMAKE_CURRENT_LIST_DIR}/../../../baselib"
    CACHE PATH "Path to the baselib source checkout")
add_subdirectory("${BASELIB_SOURCE_ROOT}" "${CMAKE_CURRENT_BINARY_DIR}/baselib")

add_executable(hello main.cc)
buildtool_target_modules(hello
  PRIVATE
  #NATIVE_MODULES
  LIBRARY baselib::baselib
  MODULES lib.fmt
)
