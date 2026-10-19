cmake_minimum_required(VERSION 3.24)
project(blk_studio VERSION 2.0.0 LANGUAGES C CXX)

### Set compiler
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
if(MSVC)
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()

### Find dependencies
find_package(Threads REQUIRED)
include(cmake/dependencies.cmake)
include(cmake/jpeg_xr.cmake)

### Build core
add_library(blk_core INTERFACE)
target_include_directories(blk_core INTERFACE blk_core/include)
target_include_directories(blk_core SYSTEM INTERFACE "${BLK_THIRD_PARTY}")
target_link_libraries(blk_core INTERFACE jpeg_xr Threads::Threads)
if(MSVC)
    target_compile_options(blk_core INTERFACE /fp:precise /utf-8)
else()
    target_compile_options(blk_core INTERFACE -ffp-contract=off)
endif()
if(WIN32)
    target_link_libraries(blk_core INTERFACE ws2_32 shell32 crypt32)
    target_compile_definitions(blk_core INTERFACE NOMINMAX WIN32_LEAN_AND_MEAN)
endif()

### Build applications
add_executable(blk_convert blk_core/src/main.cpp)
target_include_directories(blk_convert PRIVATE blk_core/src)
target_link_libraries(blk_convert PRIVATE blk_core)
add_executable(blk_studio WIN32 blk_core/src/studio.cpp)
target_include_directories(blk_studio PRIVATE blk_core/src)
target_link_libraries(blk_studio PRIVATE blk_core)
if(MINGW)
    target_link_options(blk_studio PRIVATE -static -municode)
    target_link_options(blk_convert PRIVATE -static)
endif()

### Copy runtime resources
#Codex: Refresh assets without a relink; one target avoids concurrent writes to the shared runtime directory.
add_custom_target(blk_runtime_resources ALL
    COMMAND ${CMAKE_COMMAND} -E copy_directory ${CMAKE_CURRENT_SOURCE_DIR}/web $<TARGET_FILE_DIR:blk_studio>/web
    COMMAND ${CMAKE_COMMAND} -E copy_directory ${CMAKE_CURRENT_SOURCE_DIR}/mapping $<TARGET_FILE_DIR:blk_studio>/mapping
    COMMAND ${CMAKE_COMMAND} -E copy_if_different ${CMAKE_CURRENT_SOURCE_DIR}/LICENSE $<TARGET_FILE_DIR:blk_studio>/LICENSE
    DEPENDS blk_studio blk_convert)

### Test
include(CTest)
if(BUILD_TESTING)
    add_executable(core_tests tests/core_tests.cpp)
    target_link_libraries(core_tests PRIVATE blk_core)
    if(MINGW)
        target_link_options(core_tests PRIVATE -static)
    endif()
    add_test(NAME core_tests COMMAND core_tests)
endif()

### Install
install(TARGETS blk_studio blk_convert RUNTIME DESTINATION . BUNDLE DESTINATION .)
install(DIRECTORY web DESTINATION .)
install(DIRECTORY mapping DESTINATION . FILES_MATCHING PATTERN "*.py" PATTERN "requirements.txt"
        PATTERN "__pycache__" EXCLUDE)
install(DIRECTORY mapping/bin DESTINATION mapping OPTIONAL)
install(FILES LICENSE THIRD_PARTY_NOTICES.txt README.md DESTINATION .)
install(FILES "${BLK_THIRD_PARTY}/jxrlib/LICENSE" DESTINATION licenses RENAME jxrlib.LICENSE)
install(FILES "${BLK_THIRD_PARTY}/httplib.LICENSE" "${BLK_THIRD_PARTY}/json.LICENSE" "${BLK_THIRD_PARTY}/picosha2.LICENSE" DESTINATION licenses)
