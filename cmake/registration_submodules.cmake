### Initialize pinned registration sources before configuring or launching a build.
get_filename_component(BLK_PROJECT_ROOT "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)

if(NOT EXISTS "${BLK_PROJECT_ROOT}/.git")
    # Source ZIPs use the checksum-pinned archive fallback in dependencies.cmake.
    return()
endif()

find_package(Git REQUIRED)
file(READ "${BLK_PROJECT_ROOT}/mapping/native/dependencies.json" submodule_manifest)
set(registration_submodules)
foreach(name kiss nano)
    string(JSON path GET "${submodule_manifest}" ${name} submodule)
    string(JSON commit GET "${submodule_manifest}" ${name} commit)
    execute_process(COMMAND "${GIT_EXECUTABLE}" ls-files --stage -- "${path}"
        WORKING_DIRECTORY "${BLK_PROJECT_ROOT}"
        RESULT_VARIABLE index_status OUTPUT_VARIABLE index_entry ERROR_VARIABLE index_error
        OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(NOT index_status EQUAL 0 OR NOT index_entry MATCHES "^160000 ${commit} 0\t")
        message(FATAL_ERROR "Submodule ${path} must record commit ${commit}. Keep its gitlink and dependencies.json in sync. ${index_error}")
    endif()
    list(APPEND registration_submodules "${path}")
endforeach()

execute_process(COMMAND "${GIT_EXECUTABLE}" submodule status --recursive -- ${registration_submodules}
    WORKING_DIRECTORY "${BLK_PROJECT_ROOT}"
    RESULT_VARIABLE submodule_status OUTPUT_VARIABLE submodule_state ERROR_VARIABLE submodule_error)
if(NOT submodule_status EQUAL 0 OR submodule_state MATCHES "(^|\n)U")
    message(FATAL_ERROR "Cannot read registration submodules: ${submodule_error}${submodule_state}")
endif()
if(submodule_state MATCHES "(^|\n)[-+]")
    message(STATUS "Initializing pinned registration submodules")
    execute_process(COMMAND "${GIT_EXECUTABLE}" submodule update --init --recursive --checkout -- ${registration_submodules}
        WORKING_DIRECTORY "${BLK_PROJECT_ROOT}"
        RESULT_VARIABLE update_status OUTPUT_VARIABLE update_output ERROR_VARIABLE update_error)
    if(NOT update_status EQUAL 0)
        message(FATAL_ERROR "Cannot initialize registration submodules: ${update_output}${update_error}")
    endif()
endif()

foreach(name kiss nano)
    string(JSON path GET "${submodule_manifest}" ${name} submodule)
    string(JSON commit GET "${submodule_manifest}" ${name} commit)
    execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${BLK_PROJECT_ROOT}/${path}" rev-parse HEAD
        RESULT_VARIABLE head_status OUTPUT_VARIABLE head ERROR_VARIABLE head_error
        OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(NOT head_status EQUAL 0 OR NOT head STREQUAL commit)
        message(FATAL_ERROR "Submodule ${path} did not resolve to ${commit}: ${head_error}")
    endif()
endforeach()
