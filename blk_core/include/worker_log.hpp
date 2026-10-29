#pragma once

#include <algorithm>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <json.hpp>

namespace blk_core
{
    // Byte cursors keep polling and backward paging bounded, even for multi-day runs.
    inline nlohmann::json workerLogPage(const std::filesystem::path &path,
                                       const std::string &before = "",
                                       const std::string &after = "",
                                       std::uint64_t page_size = 32768)
    {
        if (!before.empty() && !after.empty()) throw std::runtime_error("Choose one log cursor");
        const auto cursor = [](const std::string &value) -> std::uint64_t
        {
            if (value.empty() || value.find_first_not_of("0123456789") != std::string::npos)
                throw std::runtime_error("Invalid log cursor");
            return std::stoull(value);
        };
        const auto offset = !before.empty() ? cursor(before) : !after.empty() ? cursor(after) : 0;
        std::ifstream stream(path, std::ios::binary | std::ios::ate);
        const std::uint64_t size = stream && stream.tellg() >= 0 ? static_cast<std::uint64_t>(stream.tellg()) : 0;
        const bool reset = offset > size;
        std::uint64_t end = before.empty() || reset ? size : offset;
        std::uint64_t start = !after.empty() && !reset ? offset : end - std::min(end, page_size);
        end = std::min(end, start + page_size);
        std::string text(static_cast<std::size_t>(end - start), '\0');
        if (!text.empty())
        {
            stream.seekg(static_cast<std::streamoff>(start));
            stream.read(text.data(), static_cast<std::streamsize>(text.size()));
            text.resize(static_cast<std::size_t>(stream.gcount()));
            const auto continuation = [](unsigned char c) { return (c & 0xc0) == 0x80; };
            std::size_t first = 0;
            while (first < text.size() && continuation(text[first])) ++first;
            start += first;
            text.erase(0, first);
            // Do not split a UTF-8 character (including a worker's incomplete final write).
            if (!text.empty())
            {
                std::size_t lead = text.size() - 1;
                while (lead && continuation(text[lead])) --lead;
                const auto c = static_cast<unsigned char>(text[lead]);
                const std::size_t width = c < 0x80 ? 1 : c < 0xe0 ? 2 : c < 0xf0 ? 3 : 4;
                if (text.size() - lead < width) text.resize(lead);
            }
        }
        end = start + text.size();
        return {{"text", text}, {"start", start}, {"end", end}, {"size", size}, {"reset", reset}};
    }
}
