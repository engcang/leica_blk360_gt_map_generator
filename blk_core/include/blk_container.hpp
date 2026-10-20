#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include "common.hpp"

namespace blk_core
{
    struct Block
    {
        std::uint64_t offset_ = 0;
        std::uint64_t size_ = 0;
    };

    struct ScanModel
    {
        std::map<std::string, Block> blocks_;
        std::int32_t columns_ = 0;
        std::int32_t rows_ = 0;
        double range_scale_ = 0;
        double cone_scale_ = 0;
        double phi_scale_ = 0;
        std::int32_t phi_offset_ = 0;
        std::int32_t cone_offset_ = 0;
        std::vector<std::uint8_t> lines_;
    };

    class BlkContainer
    {
    public:
        explicit BlkContainer(const std::filesystem::path &_path):
            stream_(_path, std::ios::binary), size_(std::filesystem::file_size(_path))
        {
            requireLittleEndian();
            if (!stream_)
            {
                throw std::runtime_error("Cannot open BLK input");
            }
        }

        std::vector<std::uint8_t> read(std::uint64_t _offset,
                                       std::uint64_t _size)
        {
            if (_offset > size_ || _size > size_ - _offset || _size > 2147483647)
            {
                throw std::runtime_error("Invalid BLK block bounds");
            }
            stream_.clear();
            stream_.seekg(static_cast<std::streamoff>(_offset));
            auto bytes = readBytes(stream_, static_cast<std::size_t>(_size));
            for (auto &value : bytes)
            {
                value ^= 0xaa;
            }
            return bytes;
        }

        std::map<std::string, Block> ldrImages()
        {
            const auto length = std::min<std::uint64_t>(size_, 1048576);
            const auto start = size_ - length;
            const auto tail = read(start, length);
            std::map<std::string, Block> result;
            for (std::size_t i = 12; i + 40 < tail.size(); ++i) {
                if (std::memcmp(tail.data() + i, "LDR_Image_", 10) != 0) continue;
                std::size_t end = i + 10;
                while (end < tail.size() && tail[end] >= '0' && tail[end] <= '9') ++end;
                if (end == i + 10 || end >= tail.size() || tail[end] != 0) continue;
                const auto n = end - i + 1;
                if (i + n + 22 > tail.size() || readNumber<std::uint16_t>(tail, i-2) != n ||
                    readNumber<std::uint16_t>(tail, i-12) != n+32 || readNumber<std::uint32_t>(tail, i-10) != 1 || tail[i+n] != 1) continue;
                const auto offset = readNumber<std::uint64_t>(tail, i+n+1);
                const auto bytes = readNumber<std::uint64_t>(tail, i+n+9);
                if (offset < 25 || offset > size_ || !bytes || bytes > size_-offset || offset+bytes > start+i-12) continue;
                const std::string name(reinterpret_cast<const char *>(tail.data()+i), n-1);
                if (!result.emplace(name, Block{offset, bytes}).second) throw std::runtime_error("Ambiguous image directory");
            }
            if (result.empty()) throw std::runtime_error("This BLK has no supported LDR images. RGB requires an LDR scan; HDR is not supported yet.");
            return result;
        }

        ScanModel parse()
        {
            const auto prefix = read(0, 25);
            if (readNumber<std::uint16_t>(prefix, 0) != 1 || readNumber<std::uint16_t>(prefix, 2) != 15 || std::memcmp(prefix.data() + 4, "RowColReversed\0", 15) != 0 || prefix[24] != 1)
            {
                throw std::runtime_error("Unsupported BLK layout; expected BLK360 G1 RowColReversed");
            }
            ScanModel model;
            const auto tail_size = std::min<std::uint64_t>(size_, 1048576);
            const auto start = size_ - tail_size;
            const auto tail = read(start, tail_size);
            const std::array<std::string, 6> names = {"CylRanges", "ConeRes", "PhiRes", "ScanParams", "ScanLineParams", "Intensity"};
            const std::array<std::uint8_t, 6> types = {5, 6, 7, 8, 9, 1};
            for (std::size_t k = 0; k < names.size(); ++k)
            {
                const auto name = names[k] + std::string(1, '\0');
                bool found = false;
                for (std::size_t i = 12; i + name.size() + 22 <= tail.size(); ++i)
                {
                    if (std::memcmp(tail.data() + i, name.data(), name.size()) != 0)
                    {
                        continue;
                    }
                    if (readNumber<std::uint16_t>(tail, i - 2) != name.size() || readNumber<std::uint16_t>(tail, i - 12) != name.size() + 32 || readNumber<std::uint32_t>(tail, i - 10) != 1 || tail[i + name.size()] != types[k])
                    {
                        continue;
                    }
                    const auto offset = readNumber<std::uint64_t>(tail, i + name.size() + 1);
                    const auto length = readNumber<std::uint64_t>(tail, i + name.size() + 9);
                    if (offset < 25 || offset > size_ || length == 0 || length > size_ - offset || offset + length > start + i - 12)
                    {
                        continue;
                    }
                    if (found)
                    {
                        throw std::runtime_error("Ambiguous scan directory: " + names[k]);
                    }
                    found = true;
                    model.blocks_[names[k]] = {offset, length};
                }
                if (!found)
                {
                    throw std::runtime_error("Missing scan block: " + names[k]);
                }
            }
            const auto parameters = model.blocks_.at("ScanParams");
            if (parameters.size_ != 36)
            {
                throw std::runtime_error("Unsupported scan parameter layout");
            }
            const auto bytes = read(parameters.offset_, parameters.size_);
            model.columns_ = readNumber<std::int32_t>(bytes, 0);
            model.range_scale_ = readNumber<double>(bytes, 4);
            model.cone_scale_ = readNumber<double>(bytes, 12);
            model.phi_scale_ = readNumber<double>(bytes, 20);
            model.phi_offset_ = readNumber<std::int32_t>(bytes, 28);
            model.cone_offset_ = readNumber<std::int32_t>(bytes, 32);
            if (model.columns_ <= 0 || model.columns_ > 100000 || !std::isfinite(model.range_scale_) || !std::isfinite(model.cone_scale_) || !std::isfinite(model.phi_scale_) || model.range_scale_ <= 0 || model.cone_scale_ <= 0 || model.phi_scale_ <= 0)
            {
                throw std::runtime_error("Invalid scan dimensions or quantization");
            }
            const auto lines = model.blocks_.at("ScanLineParams");
            if (lines.size_ != 113ULL * model.columns_)
            {
                throw std::runtime_error("Unsupported scanline layout");
            }
            model.lines_ = read(lines.offset_, lines.size_);
            for (std::int32_t c = 0; c < model.columns_; ++c)
            {
                const auto q = static_cast<std::size_t>(c) * 113;
                if (model.lines_[q] > 1)
                {
                    throw std::runtime_error("Invalid scanline validity flag");
                }
                if (model.lines_[q] == 0)
                {
                    continue;
                }
                for (std::size_t j = 0; j < 14; ++j)
                {
                    if (!std::isfinite(readNumber<double>(model.lines_, q + 1 + 8 * j)))
                    {
                        throw std::runtime_error("Non-finite scanline model");
                    }
                }
                const auto nx = readNumber<double>(model.lines_, q + 1);
                const auto ny = readNumber<double>(model.lines_, q + 9);
                if (nx * nx + ny * ny < 1e-20)
                {
                    throw std::runtime_error("Degenerate scanline frame");
                }
            }
            return model;
        }

    private:
        std::ifstream stream_;
        std::uint64_t size_ = 0;
    };
} //namespace blk_core
