#pragma once

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string>

#include <json.hpp>

namespace blk_core
{
    inline bool hasExtension(const std::filesystem::path &_path,
                             const std::string &_extension)
    {
        auto extension = _path.extension().u8string();
        std::transform(extension.begin(), extension.end(), extension.begin(), [](unsigned char _value)
        {
            return static_cast<char>(std::tolower(_value));
        });
        return extension == _extension;
    }

    inline nlohmann::json inspectPly(const std::filesystem::path &_path)
    {
        if (!std::filesystem::is_regular_file(_path) || !hasExtension(_path, ".ply"))
        {
            throw std::runtime_error("PLY file is missing. Select it again if it was moved.");
        }
        std::ifstream stream(_path, std::ios::binary);
        if (!stream)
        {
            throw std::runtime_error("Cannot open PLY file");
        }
        std::array<char, 65536> buffer{};
        stream.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        const std::string text(buffer.data(), static_cast<std::size_t>(stream.gcount()));
        const auto marker = text.find("\nend_header\n");
        const std::string prefix = "ply\nformat binary_little_endian 1.0\n";
        const std::string suffix = "property float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n";
        if (marker == std::string::npos || text.rfind(prefix, 0) != 0)
        {
            throw std::runtime_error("Expected a BLK Studio binary XYZ/intensity PLY");
        }
        const auto header = text.substr(0, marker + 12);
        if (header.size() < prefix.size() + suffix.size() || header.compare(header.size() - suffix.size(), suffix.size(), suffix) != 0)
        {
            throw std::runtime_error("Expected float XYZ and uint16 intensity PLY properties");
        }
        std::istringstream fields(header.substr(prefix.size(), header.size() - prefix.size() - suffix.size()));
        std::string line;
        std::uint64_t count = 0;
        while (std::getline(fields, line))
        {
            if (line.rfind("comment ", 0) == 0 || line.rfind("obj_info ", 0) == 0)
            {
                continue;
            }
            if (line.rfind("element vertex ", 0) != 0 || count != 0)
            {
                throw std::runtime_error("Unsupported PLY header");
            }
            const auto digits = line.substr(15);
            if (digits.empty() || digits.find_first_not_of("0123456789") != std::string::npos)
            {
                throw std::runtime_error("Invalid PLY point count");
            }
            count = std::stoull(digits);
        }
        const auto bytes = std::filesystem::file_size(_path);
        if (count == 0 || bytes < header.size() || (bytes - header.size()) % 14 != 0 || (bytes - header.size()) / 14 != count)
        {
            throw std::runtime_error("PLY size/count mismatch; the file may be incomplete");
        }
        return {{"valid_points", count}, {"bytes", bytes}};
    }
} //namespace blk_core
