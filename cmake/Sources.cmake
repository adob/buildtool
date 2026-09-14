# Create a static archive target from SOURCES inside a registered LIBRARY.
# Paths are relative to the calling directory. Buildtool discovers all imports
# and implementation companions, using each registered library's own settings.
# PUBLIC_MODULES publishes a standalone mapper for CMake consumers of public headers.
function(buildtool_add_library name)
  if(CMAKE_VERSION VERSION_LESS 3.30)
    message(FATAL_ERROR "buildtool_add_library requires CMake 3.30 or newer")
  endif()
  if(NOT CMAKE_CXX_COMPILER_ID STREQUAL "GNU" OR WIN32 OR CMAKE_CROSSCOMPILING)
    message(FATAL_ERROR "buildtool_add_library currently requires native GCC on POSIX")
  endif()
  cmake_parse_arguments(BT "PUBLIC_MODULES" "LIBRARY" "SOURCES" ${ARGN})
  if(NOT BT_LIBRARY OR NOT BT_SOURCES OR BT_UNPARSED_ARGUMENTS)
    message(FATAL_ERROR "Use buildtool_add_library(name LIBRARY target SOURCES files...)")
  endif()
  if(TARGET "${name}" OR NOT TARGET "${BT_LIBRARY}")
    message(FATAL_ERROR "The archive name must be new and LIBRARY must be an existing target")
  endif()
  get_target_property(library ${BT_LIBRARY} BUILDTOOL_DIRECTORY)
  if(NOT library)
    message(FATAL_ERROR "LIBRARY must be registered with buildtool_register_project")
  endif()
  set(sources)
  foreach(source IN LISTS BT_SOURCES)
    if(NOT source MATCHES "\\.(cc|cpp)$" OR source MATCHES "\\$<")
      message(FATAL_ERROR "SOURCES must be .cc or .cpp paths without generator expressions: ${source}")
    endif()
    get_filename_component(source "${source}" ABSOLUTE BASE_DIR "${CMAKE_CURRENT_SOURCE_DIR}")
    list(APPEND sources "${source}")
  endforeach()

  set(directory "${CMAKE_CURRENT_BINARY_DIR}/buildtool/${name}_buildtool_sources/$<CONFIG>")
  _buildtool_import_archive(${name} ${BT_LIBRARY} "${directory}")
  set(registry "${CMAKE_BINARY_DIR}/buildtool/libraries")
  set(reply_directory "${CMAKE_BINARY_DIR}/.cmake/api/v1/reply")
  set(configuration "$<CONFIG>")
  foreach(field library registry reply_directory configuration sources)
    file(GENERATE OUTPUT "${directory}/${field}"
      CONTENT "$<JOIN:${${field}},\n>\n")
  endforeach()
  set(metadata_outputs)
  if(BT_PUBLIC_MODULES)
    file(GENERATE OUTPUT "${directory}/native_modules" CONTENT "FALSE\n")
    set_target_properties(${name} PROPERTIES
      INTERFACE_COMPILE_OPTIONS "$<$<COMPILE_LANGUAGE:CXX>:@${directory}/consumer.rsp>"
      INTERFACE_BUILDTOOL_BUNDLE "${name}"
      COMPATIBLE_INTERFACE_STRING BUILDTOOL_BUNDLE)
    list(APPEND metadata_outputs "${directory}/consumer.rsp" "${directory}/state.h"
      "${directory}/providers.json" "${directory}/consumer.modmap")
  endif()
  file(GENERATE OUTPUT "${directory}/public_modules" CONTENT "${BT_PUBLIC_MODULES}\n")
  _buildtool_add_build_job(${name} ${BT_LIBRARY} "${directory}" "Building ${name} sources"
    ${metadata_outputs})
endfunction()

# Build application SOURCES with buildtool and link name with CMake.
# LIBRARY supplies registered project settings; PRIVATE_LIBRARIES adds application
# compile/link dependencies. DEPENDS lists targets producing required input files.
# Source paths are relative to the calling directory, which owns application settings.
function(buildtool_add_executable name)
  cmake_parse_arguments(BT "" "LIBRARY" "SOURCES;PRIVATE_LIBRARIES;DEPENDS" ${ARGN})
  if(NOT BT_LIBRARY OR NOT BT_SOURCES OR BT_UNPARSED_ARGUMENTS OR BT_KEYWORDS_MISSING_VALUES)
    message(FATAL_ERROR
      "Use buildtool_add_executable(name LIBRARY target SOURCES files... [PRIVATE_LIBRARIES targets...] [DEPENDS targets...])")
  endif()
  if(TARGET "${name}" OR NOT TARGET "${BT_LIBRARY}")
    message(FATAL_ERROR "The executable name must be new and LIBRARY must be an existing target")
  endif()
  get_target_property(library ${BT_LIBRARY} BUILDTOOL_DIRECTORY)
  if(NOT library)
    message(FATAL_ERROR "LIBRARY must be registered with buildtool_register_project")
  endif()

  set(settings "${name}_buildtool_settings")
  set(archive "${name}_buildtool_archive")
  buildtool_register_project(${settings} SOURCE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}")
  # These usage requirements flow through the internal archive to the final link.
  target_link_libraries(${settings} PUBLIC ${BT_LIBRARY} ${BT_PRIVATE_LIBRARIES})
  buildtool_add_library(${archive} LIBRARY ${settings} SOURCES ${BT_SOURCES})
  if(BT_DEPENDS)
    add_dependencies(${archive}_build ${BT_DEPENDS})
  endif()

  # CMake requires an executable source; main and all real sources are in the archive.
  set(link_source "${CMAKE_CURRENT_BINARY_DIR}/buildtool/${name}/link.cc")
  file(GENERATE OUTPUT "${link_source}"
    CONTENT "// Application sources are compiled by buildtool.\n")
  add_executable(${name} "${link_source}")
  set_property(TARGET ${name} PROPERTY CXX_SCAN_FOR_MODULES OFF)
  target_link_libraries(${name} PRIVATE ${archive})
endfunction()
