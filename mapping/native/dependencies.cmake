### Fetch pinned sources
include(FetchContent)
file(READ "${CMAKE_CURRENT_LIST_DIR}/dependencies.json" dependency_manifest)
string(JSON dependency_count LENGTH "${dependency_manifest}")
math(EXPR last_dependency "${dependency_count} - 1")
foreach(index RANGE ${last_dependency})
    string(JSON name MEMBER "${dependency_manifest}" ${index})
    string(JSON submodule ERROR_VARIABLE not_submodule GET "${dependency_manifest}" ${name} submodule)
    if(NOT not_submodule)
        if(EXISTS "${BLK_PROJECT_ROOT}/.git")
            continue()
        endif()
        set(source_directory "${BLK_PROJECT_ROOT}/${submodule}")
    elseif(BLK_DOWNLOAD_REGISTRATION_DEPS)
        set(source_directory "${BLK_REGISTRATION_DEPS}/${name}")
    else()
        continue()
    endif()
    string(JSON url GET "${dependency_manifest}" ${name} url)
    string(JSON sha256 GET "${dependency_manifest}" ${name} sha256)
    FetchContent_Declare(blk_dependency_${name}
        URL "${url}"
        URL_HASH "SHA256=${sha256}"
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE
        SOURCE_DIR "${source_directory}"
        SOURCE_SUBDIR blk_unused_subdirectory)
    FetchContent_MakeAvailable(blk_dependency_${name})
endforeach()
