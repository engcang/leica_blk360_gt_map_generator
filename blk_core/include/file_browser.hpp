#pragma once

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <vector>

#include <json.hpp>

#include "platform.hpp"
#include "project.hpp"

namespace blk_core
{
    inline nlohmann::json browseFiles(const std::string &_directory,
                                      const std::string &_extension = ".blk")
    {
#ifdef _WIN32
        std::vector<wchar_t> home_buffer(32768);
        const auto length = GetEnvironmentVariableW(L"USERPROFILE", home_buffer.data(), static_cast<DWORD>(home_buffer.size()));
        const auto home_path = length > 0 && length < home_buffer.size() ? std::filesystem::path(home_buffer.data()) : std::filesystem::current_path();
#else
        const auto *home = std::getenv("HOME");
        const auto home_path = home ? std::filesystem::u8path(home) : std::filesystem::current_path();
#endif
        const auto path = std::filesystem::canonical(_directory.empty() ? home_path : std::filesystem::u8path(_directory));
        if (!std::filesystem::is_directory(path))
        {
            throw std::runtime_error("Choose a directory");
        }
        std::vector<nlohmann::json> entries;
        for (const auto &entry : std::filesystem::directory_iterator(path, std::filesystem::directory_options::skip_permission_denied))
        {
            std::error_code error;
            const bool directory = entry.is_directory(error);
            if (error || (!directory && (!entry.is_regular_file(error) || !hasExtension(entry.path(), _extension))))
            {
                continue;
            }
            entries.push_back({{"name", entry.path().filename().u8string()}, {"path", entry.path().u8string()}, {"directory", directory}, {"modified_time", modifiedTime(entry.path())}});
        }
        std::sort(entries.begin(), entries.end(), [](const nlohmann::json &_a, const nlohmann::json &_b)
        {
            if (_a["directory"] != _b["directory"])
            {
                return _a["directory"].get<bool>();
            }
            if (!_a["directory"].get<bool>() && _a["modified_time"] != _b["modified_time"])
                return _a["modified_time"].get<std::int64_t>() < _b["modified_time"].get<std::int64_t>();
            return _a["name"].get<std::string>() < _b["name"].get<std::string>();
        });
        auto roots = nlohmann::json::array();
        roots.push_back({{"name", "Home"}, {"path", home_path.u8string()}});
#ifdef _WIN32
        const auto mask = GetLogicalDrives();
        for (int i = 0; i < 26; ++i)
        {
            if (mask & (1UL << i))
            {
                const auto drive = std::string(1, static_cast<char>('A' + i)) + ":/";
                roots.push_back({{"name", drive}, {"path", drive}});
            }
        }
#else
        roots.push_back({{"name", "Filesystem"}, {"path", "/"}});
#endif
        return {{"path", path.u8string()}, {"parent", path.parent_path().u8string()}, {"entries", entries}, {"roots", roots}};
    }
} //namespace blk_core
