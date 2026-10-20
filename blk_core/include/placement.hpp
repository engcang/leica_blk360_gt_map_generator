#pragma once

#include <cmath>
#include <cstring>
#include <vector>
#include "ply.hpp"

namespace blk_core
{
    inline void validatePlacement(const nlohmann::json &_pose)
    {
        if (_pose.is_null())
            return;
        if (!_pose.is_array() || _pose.size() != 4)
            throw std::runtime_error("Placement needs X, Y, Z and yaw");
        for (const auto &value : _pose)
        {
            if (!value.is_number() || !std::isfinite(value.get<double>()) || std::abs(value.get<double>()) > 1e6)
                throw std::runtime_error("Placement values must be finite and within supported bounds");
        }
    }

    // Sample in memory directly from the original PLY; never create a cached PLY copy.
    inline std::string placementSample(const std::filesystem::path &_path, std::uint64_t _start, std::uint64_t _stride)
    {
        if (_stride != 1 && _stride != 2 && _stride != 5 && _stride != 10 && _stride != 50)
            throw std::runtime_error("Unsupported display density");
        const auto metadata = inspectPly(_path);
        const auto count = metadata.at("valid_points").get<std::uint64_t>();
        const auto total = (count + _stride - 1) / _stride;
        if (_start >= total)
            throw std::runtime_error("Sample offset is outside the PLY");
        const auto take = std::min<std::uint64_t>(262144, total - _start);
        const auto header = metadata.at("bytes").get<std::uint64_t>() - count * plyStride;
        std::ifstream stream(_path, std::ios::binary);
        stream.seekg(static_cast<std::streamoff>(header + _start * _stride * plyStride));
        std::string output(static_cast<std::size_t>(take * 16), '\0');
        std::vector<char> block(65536 * plyStride);
        std::uint64_t read = 0, emitted = 0;
        const auto remaining = std::min(count - _start * _stride, take * _stride);
        while (read < remaining)
        {
            const auto records = std::min<std::uint64_t>(65536, remaining - read);
            stream.read(block.data(), static_cast<std::streamsize>(records * plyStride));
            if (!stream)
                throw std::runtime_error("Incomplete PLY sample read");
            for (std::uint64_t q = 0; q < records; ++q)
            {
                if ((read + q) % _stride != 0)
                    continue;
                float values[4];
                std::memcpy(values, block.data() + q * plyStride, 12);
                std::uint16_t intensity;
                std::memcpy(&intensity, block.data() + q * plyStride + 12, 2);
                values[3] = intensity;
                if (rgbEdition) {
                    const auto *rgb = reinterpret_cast<const unsigned char *>(block.data() + q * plyStride + 12);
                    values[3] = static_cast<float>((rgb[0] << 16) | (rgb[1] << 8) | rgb[2]);
                }
                for (int axis = 0; axis < 3; ++axis)
                    if (!std::isfinite(values[axis]))
                        throw std::runtime_error("Non-finite PLY coordinates");
                std::memcpy(output.data() + emitted * 16, values, 16);
                ++emitted;
            }
            read += records;
        }
        if (emitted != take)
            throw std::runtime_error("PLY sample count changed");
        return output;
    }
} //namespace blk_core
