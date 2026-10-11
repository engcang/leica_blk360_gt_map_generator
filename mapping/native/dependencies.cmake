### Fetch pinned sources
include(FetchContent)
file(READ "${CMAKE_CURRENT_LIST_DIR}/dependencies.json" dependency_manifest)
set(BLK_REGISTRATION_DEPS "${CMAKE_CURRENT_BINARY_DIR}/sources")
string(JSON dependency_count LENGTH "${dependency_manifest}")
math(EXPR last_dependency "${dependency_count} - 1")
foreach(index RANGE ${last_dependency})
    string(JSON name MEMBER "${dependency_manifest}" ${index})
    string(JSON url GET "${dependency_manifest}" ${name} url)
    string(JSON sha256 GET "${dependency_manifest}" ${name} sha256)
    FetchContent_Declare(blk_dependency_${name}
        URL "${url}"
        URL_HASH "SHA256=${sha256}"
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE
        SOURCE_DIR "${BLK_REGISTRATION_DEPS}/${name}"
        SOURCE_SUBDIR blk_unused_subdirectory)
    FetchContent_MakeAvailable(blk_dependency_${name})
endforeach()
