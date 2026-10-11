### Pinned dependency cache
include(FetchContent)
set(BLK_THIRD_PARTY "${CMAKE_CURRENT_SOURCE_DIR}/third_party" CACHE PATH "Downloaded, unmodified third-party source cache")
file(MAKE_DIRECTORY "${BLK_THIRD_PARTY}")

function(downloadDependency _name _url _sha256)
    set(destination "${BLK_THIRD_PARTY}/${_name}")
    if(EXISTS "${destination}")
        file(SHA256 "${destination}" actual_hash)
        if(actual_hash STREQUAL _sha256)
            return()
        endif()
        message(FATAL_ERROR "Unexpected dependency contents: ${destination}. Move the modified file aside and configure again.")
    endif()
    message(STATUS "Downloading pinned dependency: ${_name}")
    file(DOWNLOAD "${_url}" "${destination}.pending"
        EXPECTED_HASH "SHA256=${_sha256}" TLS_VERIFY ON TIMEOUT 120 STATUS download_status)
    list(GET download_status 0 download_code)
    if(NOT download_code EQUAL 0)
        message(FATAL_ERROR "Dependency download failed for ${_name}: ${download_status}")
    endif()
    file(RENAME "${destination}.pending" "${destination}")
endfunction()

### Header-only libraries and their notices
downloadDependency(httplib.h "https://raw.githubusercontent.com/yhirose/cpp-httplib/v0.18.7/httplib.h"
    4770f8ea3d3fcd27b0b713c783d9b53bce3baddbd5adad13714011df96142526)
downloadDependency(httplib.LICENSE "https://raw.githubusercontent.com/yhirose/cpp-httplib/v0.18.7/LICENSE"
    4b45cbe16d7b71b89ae6127e26e0d90a029198ca5e958ad8e3d0b8bbed364d8b)
downloadDependency(json.hpp "https://raw.githubusercontent.com/nlohmann/json/v3.12.0/single_include/nlohmann/json.hpp"
    aaf127c04cb31c406e5b04a63f1ae89369fccde6d8fa7cdda1ed4f32dfc5de63)
downloadDependency(json.LICENSE "https://raw.githubusercontent.com/nlohmann/json/v3.12.0/LICENSE.MIT"
    46a65cffd1ea955132d95a8dd921640714a8d6b537d2e4e482d31145ae95b603)
downloadDependency(picosha2.h "https://raw.githubusercontent.com/okdshin/PicoSHA2/v1.0.1/picosha2.h"
    b13c180161ffac8d0adc81e033e493c409457c4d1258ab9781ac80579ba3bdd8)
downloadDependency(picosha2.LICENSE "https://raw.githubusercontent.com/okdshin/PicoSHA2/v1.0.1/LICENSE"
    6c30eb1f37554ec4199cb82a8c86d9e7a852da78a757832bebb03ac9ddc44ff1)

### JPEG XR reference codec
FetchContent_Declare(blk_jxrlib
    URL "https://codeload.github.com/4creators/jxrlib/tar.gz/f7521879862b9085318e814c6157490dd9dbbdb4"
    URL_HASH SHA256=8ab20a21f6c3f8cfdedd2511b03a2009e52cce7fa6550202c355063d0d83213e
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
    SOURCE_DIR "${BLK_THIRD_PARTY}/jxrlib"
    SOURCE_SUBDIR blk_unused_subdirectory)
FetchContent_MakeAvailable(blk_jxrlib)
