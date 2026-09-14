# Register a source project without enumerating or building its modules.
# SOURCE_ROOT is the directory against which dotted module names are resolved.
# Configure name with ordinary PRIVATE/PUBLIC build and usage requirements.
function(buildtool_register_project name)
  cmake_parse_arguments(BT "" "SOURCE_ROOT" "" ${ARGN})
  if(NOT BT_SOURCE_ROOT OR BT_UNPARSED_ARGUMENTS)
    message(FATAL_ERROR "buildtool_register_project requires SOURCE_ROOT")
  endif()
  get_filename_component(root "${BT_SOURCE_ROOT}" ABSOLUTE)
  if(NOT IS_DIRECTORY "${root}")
    message(FATAL_ERROR "buildtool SOURCE_ROOT is not a directory: ${root}")
  endif()
  # A compilation target lets CMake evaluate toolchain/configuration flags and
  # transitive requirements for this library, independently of its consumers.
  add_library(${name} OBJECT EXCLUDE_FROM_ALL "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/Settings.cc")
  target_compile_features(${name} PUBLIC cxx_std_20)
  set_target_properties(${name} PROPERTIES
    CXX_SCAN_FOR_MODULES OFF
    TRANSITIVE_COMPILE_PROPERTIES BUILDTOOL_SOURCE_ROOTS
    BUILDTOOL_SOURCE_ROOTS "${root}"
    INTERFACE_BUILDTOOL_SOURCE_ROOTS "${root}")
  target_include_directories(${name} PUBLIC "${root}")

  set(directory "${CMAKE_BINARY_DIR}/buildtool/libraries/${name}/$<CONFIG>")
  set_property(TARGET ${name} PROPERTY BUILDTOOL_DIRECTORY "${directory}")
  # Module outputs are discovered at build time, so clean their shared cache
  # directories. Keep generated manifests and lock files outside those directories.
  set_property(TARGET ${name} APPEND PROPERTY ADDITIONAL_CLEAN_FILES
    "${directory}/artifacts" "${directory}/archives")
  set(compiler "${CMAKE_CXX_COMPILER}")
  set(compiler_arg1 "${CMAKE_CXX_COMPILER_ARG1}")
  set(archiver "${CMAKE_AR}")
  set(ranlib "${CMAKE_RANLIB}")
  set(reply_directory "${CMAKE_BINARY_DIR}/.cmake/api/v1/reply")
  set(configuration "$<CONFIG>")
  set(roots "$<TARGET_PROPERTY:${name},BUILDTOOL_SOURCE_ROOTS>")
  foreach(field name root roots compiler compiler_arg1 archiver ranlib reply_directory configuration)
    file(GENERATE OUTPUT "${directory}/${field}"
      CONTENT "$<JOIN:${${field}},\n>\n" TARGET ${name})
  endforeach()
  cmake_file_api(QUERY API_VERSION 1 CODEMODEL 2)
endfunction()

# Build MODULES from LIBRARY for an existing native CMake target named consumer.
# PUBLIC/PRIVATE controls propagation. Call once per consumer, listing all modules.
# Native integration defaults on for consumers with scanning enabled or module sets.
# NATIVE_MODULES explicitly enables experimental Ninja metadata integration.
# Buildtool shares any advertised jobserver; CMake compiles the consumer.
function(buildtool_target_modules consumer)
  if(CMAKE_VERSION VERSION_LESS 3.30)
    message(FATAL_ERROR "buildtool_target_modules requires CMake 3.30 or newer")
  endif()
  if(NOT CMAKE_CXX_COMPILER_ID STREQUAL "GNU" OR WIN32 OR CMAKE_CROSSCOMPILING)
    message(FATAL_ERROR "buildtool_target_modules currently requires native GCC on POSIX")
  endif()
  cmake_parse_arguments(BT "PUBLIC;PRIVATE;NATIVE_MODULES" "LIBRARY" "MODULES" ${ARGN})
  if(NOT BT_LIBRARY OR NOT BT_MODULES OR BT_UNPARSED_ARGUMENTS OR
      (BT_PUBLIC AND BT_PRIVATE) OR (NOT BT_PUBLIC AND NOT BT_PRIVATE))
    message(FATAL_ERROR "Use buildtool_target_modules(target PUBLIC|PRIVATE LIBRARY target MODULES names...)")
  endif()
  if(NOT TARGET "${consumer}" OR NOT TARGET "${BT_LIBRARY}")
    message(FATAL_ERROR "The consumer and LIBRARY must be existing CMake targets")
  endif()
  get_target_property(kind ${consumer} TYPE)
  get_target_property(imported ${consumer} IMPORTED)
  get_target_property(aliased ${consumer} ALIASED_TARGET)
  if(imported OR aliased OR NOT kind MATCHES "^(EXECUTABLE|STATIC_LIBRARY|SHARED_LIBRARY|MODULE_LIBRARY)$")
    message(FATAL_ERROR "The consumer must be an ordinary CMake executable or library")
  endif()
  # CMAKE_CXX_SCAN_FOR_MODULES initializes this property when a target is created.
  # An unset property alone is not evidence that the project uses native modules.
  get_target_property(scan_modules ${consumer} CXX_SCAN_FOR_MODULES)
  get_target_property(module_sets ${consumer} CXX_MODULE_SETS)
  if(scan_modules OR module_sets)
    set(BT_NATIVE_MODULES TRUE)
  endif()
  if(BT_NATIVE_MODULES AND NOT CMAKE_GENERATOR MATCHES "^Ninja")
    message(FATAL_ERROR
      "Native module integration for ${consumer} requires Ninja or Ninja Multi-Config "
      "(selected by NATIVE_MODULES, CXX_SCAN_FOR_MODULES, or a CXX_MODULES file set)")
  endif()
  set(bundle "${consumer}_buildtool_modules")
  if(TARGET "${bundle}")
    message(FATAL_ERROR "Call buildtool_target_modules only once per consumer; list all requested MODULES")
  endif()
  get_target_property(library ${BT_LIBRARY} BUILDTOOL_DIRECTORY)
  if(NOT library)
    message(FATAL_ERROR "LIBRARY must be registered with buildtool_register_project")
  endif()
  # Language requirements come from the library's PUBLIC compile features.
  set_property(TARGET ${consumer} PROPERTY CXX_SCAN_FOR_MODULES ${BT_NATIVE_MODULES})

  set(directory "${CMAKE_CURRENT_BINARY_DIR}/buildtool/${bundle}/$<CONFIG>")
  set(archive "${directory}/libmodules.a")
  add_library(${bundle} STATIC IMPORTED GLOBAL)
  set_target_properties(${bundle} PROPERTIES
    INTERFACE_COMPILE_OPTIONS "$<$<COMPILE_LANGUAGE:CXX>:@${directory}/consumer.rsp>"
    INTERFACE_LINK_LIBRARIES "${BT_LIBRARY}"
    INTERFACE_BUILDTOOL_BUNDLE "${bundle}"
    COMPATIBLE_INTERFACE_STRING BUILDTOOL_BUNDLE)
  # Imported locations are configuration properties, not generator expressions.
  if(CMAKE_CONFIGURATION_TYPES)
    foreach(config IN LISTS CMAKE_CONFIGURATION_TYPES)
      string(TOUPPER "${config}" upper)
      set_property(TARGET ${bundle} APPEND PROPERTY IMPORTED_CONFIGURATIONS "${config}")
      set_property(TARGET ${bundle} PROPERTY IMPORTED_LOCATION_${upper}
        "${CMAKE_CURRENT_BINARY_DIR}/buildtool/${bundle}/${config}/libmodules.a")
    endforeach()
  else()
    set_property(TARGET ${bundle} PROPERTY IMPORTED_LOCATION
      "${CMAKE_CURRENT_BINARY_DIR}/buildtool/${bundle}/${CMAKE_BUILD_TYPE}/libmodules.a")
  endif()
  if(BT_PUBLIC)
    target_link_libraries(${consumer} PUBLIC ${bundle})
  else()
    target_link_libraries(${consumer} PRIVATE ${bundle})
  endif()

  set(reply_directory "${CMAKE_BINARY_DIR}/.cmake/api/v1/reply")
  set(registry "${CMAKE_BINARY_DIR}/buildtool/libraries")
  set(configuration "$<CONFIG>")
  set(native_modules "${BT_NATIVE_MODULES}")
  set(modules "${BT_MODULES}")
  foreach(field library registry reply_directory configuration consumer modules native_modules)
    file(GENERATE OUTPUT "${directory}/${field}"
      CONTENT "$<JOIN:${${field}},\n>\n" TARGET ${consumer})
  endforeach()
  set(metadata_outputs)
  if(BT_NATIVE_MODULES)
    list(APPEND metadata_outputs "${directory}/CXXModules.json")
  else()
    list(APPEND metadata_outputs "${directory}/consumer.modmap")
  endif()
  add_custom_target(${bundle}_build
    COMMENT "Building ${BT_LIBRARY} modules"
    COMMAND "${Python3_EXECUTABLE}"
      "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/../cmake_modules.py" "${directory}"
    BYPRODUCTS "${archive}" "${directory}/consumer.rsp" "${directory}/state.h"
      "${directory}/providers.json"
      ${metadata_outputs}
    JOB_SERVER_AWARE TRUE
    VERBATIM)
  add_dependencies(${bundle}_build ${BT_LIBRARY})
  add_dependencies(${bundle} ${bundle}_build)
  # Also order direct consumer compilation explicitly, including Ninja generators.
  add_dependencies(${consumer} ${bundle}_build)
endfunction()
