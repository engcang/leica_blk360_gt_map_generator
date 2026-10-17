#pragma once

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <limits>
#include <random>
#include <stdexcept>
#include <string>
#include <vector>

#include <picosha2.h>

#include "blk_container.hpp"
#include "jpeg_xr.hpp"
#include "platform.hpp"

namespace blk_core
{
    inline std::string plyHeader(std::uint64_t _count)
    {
        return "ply\nformat binary_little_endian 1.0\ncomment BLK Studio 0.3 experimental decoder\ncomment metres scanner-local; no registration or leveling\nelement vertex " + std::to_string(_count) + "\nproperty float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n";
    }

    inline void writePoint(std::ostream &_stream,
                           const Point &_point)
    {
        std::array<char, 14> bytes;
        std::memcpy(bytes.data(), &_point.x_, 4);
        std::memcpy(bytes.data() + 4, &_point.y_, 4);
        std::memcpy(bytes.data() + 8, &_point.z_, 4);
        std::memcpy(bytes.data() + 12, &_point.intensity_, 2);
        _stream.write(bytes.data(), bytes.size());
    }

    inline Point reconstruct(const ScanModel &_model,
                             const std::array<double, 14> &_line,
                             int _row,
                             std::int32_t _range,
                             std::int32_t _cone,
                             std::int32_t _phi,
                             std::uint16_t _intensity)
    {
        const auto &v = _line;
        const double t = static_cast<double>(_row) / _model.rows_;
        const double r = _range * _model.range_scale_;
        const double phi = ((v[6] * t + v[5]) * t + v[4]) * t + v[3] + (static_cast<std::int64_t>(_phi) + _model.phi_offset_) * _model.phi_scale_;
        const double ly = r * std::cos(phi);
        const double lz = r * std::sin(phi);
        const double lx = v[7] + v[8] * r - v[9] * ly + v[10] * lz - v[11] * t * ly + v[12] * t * lz + v[13] * t + (static_cast<std::int64_t>(_cone) + _model.cone_offset_) * _model.cone_scale_;
        const double nx = v[0];
        const double ny = v[1];
        const double nz = v[2];
        const double nn = nx * nx + ny * ny + nz * nz;
        const double bn = nx * nx + ny * ny;
        Point point;
        point.x_ = static_cast<float>(nx * lx / nn - ny * ly / bn - nz * nx * lz / (bn * nn));
        point.y_ = static_cast<float>(ny * lx / nn + nx * ly / bn - nz * ny * lz / (bn * nn));
        point.z_ = static_cast<float>(nz * lx / nn + lz / nn);
        point.intensity_ = _intensity;
        if (!std::isfinite(point.x_) || !std::isfinite(point.y_) || !std::isfinite(point.z_))
        {
            throw std::runtime_error("Non-finite Cartesian coordinate");
        }
        return point;
    }

    inline std::string hashFile(const std::filesystem::path &_path,
                                const std::atomic<bool> &_cancel)
    {
        std::ifstream stream(_path, std::ios::binary);
        if (!stream)
        {
            throw std::runtime_error("Cannot hash source");
        }
        picosha2::hash256_one_by_one hasher;
        std::vector<char> bytes(1048576);
        while (stream)
        {
            checkCancel(_cancel);
            stream.read(bytes.data(), bytes.size());
            hasher.process(bytes.begin(), bytes.begin() + stream.gcount());
        }
        if (!stream.eof())
        {
            throw std::runtime_error("Source read failed");
        }
        hasher.finish();
        return picosha2::get_hash_hex_string(hasher);
    }

    class Converter
    {
    public:
        nlohmann::json convert(const std::filesystem::path &_input,
                               const std::filesystem::path &_destination,
                               const std::atomic<bool> &_cancel,
                               const std::function<void(const Progress &)> &_progress) const
        {
            requireLittleEndian();
            if (_destination.extension() != ".ply")
            {
                throw std::runtime_error("Output must end in .ply");
            }
            if (std::filesystem::exists(_destination))
            {
                throw std::runtime_error("Output already exists: " + _destination.u8string());
            }
            const auto source_size = std::filesystem::file_size(_input);
            const auto source_time = std::filesystem::last_write_time(_input);
            BlkContainer container(_input);
            const auto display_name = _input.filename().u8string();
            auto model = container.parse();
            const auto work = _destination.parent_path() / (".blk-work-" + newId());
            std::filesystem::create_directories(work);
            try
            {
                _progress({"Hashing source", 0.01});
                const auto source_hash = hashFile(_input, _cancel);
                const std::array<std::string, 4> names = {"CylRanges", "ConeRes", "PhiRes", "Intensity"};
                for (std::size_t i = 0; i < names.size(); ++i)
                {
                    checkCancel(_cancel);
                    _progress({"Decoding " + names[i], 0.05 + i * 0.12});
                    const auto block = model.blocks_.at(names[i]);
                    auto encoded = container.read(block.offset_, block.size_);
                    JpegXrDecoder decoder;
                    const auto raw = decoder.decode(encoded, i == 3 ? 16 : 32, model.rows_, model.columns_);
                    checkCancel(_cancel);
                    std::ofstream output(work / (names[i] + ".raw"), std::ios::binary);
                    output.write(reinterpret_cast<const char *>(raw.data()), static_cast<std::streamsize>(raw.size()));
                    output.close();
                    if (!output)
                    {
                        throw std::runtime_error("Cannot write decompressed scan plane; check free disk space");
                    }
                }
                _progress({"Counting valid points", 0.54});
                std::uint64_t count = 0;
                {
                    std::ifstream ranges(work / "CylRanges.raw", std::ios::binary);
                    for (int column = 0; column < model.columns_; ++column)
                    {
                        checkCancel(_cancel);
                        const auto bytes = readBytes(ranges, static_cast<std::size_t>(model.rows_) * 4);
                        for (int row = 0; row < model.rows_; ++row)
                        {
                            const auto range = readNumber<std::int32_t>(bytes, row * 4);
                            if (range < 0)
                            {
                                throw std::runtime_error("Unexpected negative cylindrical range");
                            }
                            if (range > 0 && model.lines_[column * 113])
                            {
                                ++count;
                            }
                        }
                    }
                }
                if (count == 0)
                {
                    throw std::runtime_error("Scan contains no valid nonzero points");
                }
                std::array<std::ifstream, 4> inputs;
                for (std::size_t i = 0; i < names.size(); ++i)
                {
                    inputs[i].open(work / (names[i] + ".raw"), std::ios::binary);
                }
                std::ofstream ply(work / "cloud.ply", std::ios::binary);
                ply.exceptions(std::ios::badbit | std::ios::failbit);
                ply << plyHeader(count);
                std::array<double, 3> minimum = {INFINITY, INFINITY, INFINITY};
                std::array<double, 3> maximum = {-INFINITY, -INFINITY, -INFINITY};
                std::uint64_t written = 0;
                for (int column = 0; column < model.columns_; ++column)
                {
                    checkCancel(_cancel);
                    const auto ranges = readBytes(inputs[0], model.rows_ * 4);
                    const auto cones = readBytes(inputs[1], model.rows_ * 4);
                    const auto phis = readBytes(inputs[2], model.rows_ * 4);
                    const auto intensities = readBytes(inputs[3], model.rows_ * 2);
                    const auto offset = static_cast<std::size_t>(column) * 113;
                    if (model.lines_[offset] == 0)
                    {
                        continue;
                    }
                    std::array<double, 14> coefficients;
                    for (std::size_t i = 0; i < coefficients.size(); ++i)
                    {
                        coefficients[i] = readNumber<double>(model.lines_, offset + 1 + i * 8);
                    }
                    for (int row = 0; row < model.rows_; ++row)
                    {
                        const auto range = readNumber<std::int32_t>(ranges, row * 4);
                        if (range == 0)
                        {
                            continue;
                        }
                        const auto point = reconstruct(model, coefficients, row, range, readNumber<std::int32_t>(cones, row * 4), readNumber<std::int32_t>(phis, row * 4), readNumber<std::uint16_t>(intensities, row * 2));
                        writePoint(ply, point);
                        ++written;
                        const std::array<double, 3> xyz = {point.x_, point.y_, point.z_};
                        for (std::size_t i = 0; i < 3; ++i)
                        {
                            minimum[i] = std::min(minimum[i], xyz[i]);
                            maximum[i] = std::max(maximum[i], xyz[i]);
                        }
                    }
                    if (column % 100 == 0)
                    {
                        _progress({"Writing Cartesian points", 0.57 + 0.36 * column / model.columns_});
                    }
                }
                ply.close();
                for (auto &input : inputs)
                {
                    input.close();
                }
                if (written != count || std::filesystem::file_size(work / "cloud.ply") != plyHeader(count).size() + count * 14)
                {
                    throw std::runtime_error("Output count/size verification failed");
                }
                checkCancel(_cancel);
                _progress({"Finishing PLY", 0.96});
                if (std::filesystem::file_size(_input) != source_size || std::filesystem::last_write_time(_input) != source_time)
                {
                    throw std::runtime_error("Source BLK changed during conversion; retry with a completed scan");
                }
                nlohmann::json result = {{"schema_version", 1},
                                         {"decoder_version", "0.3.0-experimental"},
                                         {"source_name", display_name},
                                         {"source_sha256", source_hash},
                                         {"source_bytes", std::filesystem::file_size(_input)},
                                         {"rows", model.rows_},
                                         {"columns", model.columns_},
                                         {"units", "metres"},
                                         {"coordinate_frame", "scanner_local"},
                                         {"valid_points", count},
                                         {"full_resolution", true},
                                         {"minimum_xyz", minimum},
                                         {"maximum_xyz", maximum},
                                         {"independently_validated", false},
                                         {"transform_map_from_scan", nullptr},
                                         {"registration_status", "not_registered"}};
                for (const auto &name : names)
                {
                    std::filesystem::remove(work / (name + ".raw"));
                }
                checkCancel(_cancel);
                // Preserve acquisition/download chronology across BLK and PLY selection.
                std::filesystem::last_write_time(work / "cloud.ply", source_time);
                publishFile(work / "cloud.ply", _destination);
                std::filesystem::remove(work);
                _progress({"Completed", 1});
                return result;
            }
            catch (...)
            {
                std::error_code error;
                std::filesystem::remove_all(work, error);
                throw;
            }
        }
    };
} //namespace blk_core
