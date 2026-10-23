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
#include <map>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <json.hpp>

#include "image_writer.hpp"
#include "platform.hpp"
#include "rgb_colour.hpp"

namespace blk_core
{
    inline constexpr double panoramaPi = 3.14159265358979323846;
    inline constexpr double panoramaFarRange = 1000.0;

    struct ExportOptions
    {
        bool panoramas_ = false;
        bool camera_images_ = false;
        int panorama_width_ = 8192;
    };

    struct ExportPaths
    {
        std::filesystem::path colour_;
        std::filesystem::path intensity_;
        std::filesystem::path images_;
    };

    inline bool anyExport(const ExportOptions &_options)
    {
        return _options.panoramas_ || _options.camera_images_;
    }

    inline ExportOptions parseExportOptions(const std::vector<std::string> &_flags)
    {
        ExportOptions options;
        for (std::size_t i = 0; i < _flags.size(); ++i)
        {
            const auto &flag = _flags[i];
            const bool width = flag == "--panorama-width" && i + 1 < _flags.size() && (_flags[i + 1] == "4096" || _flags[i + 1] == "8192" || _flags[i + 1] == "16384");
            if (flag == "--panoramas")
            {
                options.panoramas_ = true;
            }
            else if (flag == "--camera-images")
            {
                options.camera_images_ = true;
            }
            else if (width)
            {
                options.panorama_width_ = std::stoi(_flags[++i]);
            }
            else
            {
                throw std::runtime_error("Options: --panoramas --camera-images --panorama-width 4096|8192|16384");
            }
        }
        return options;
    }

    inline ExportPaths exportPaths(const std::filesystem::path &_ply)
    {
        const auto stem = _ply.stem().u8string();
        const auto folder = _ply.parent_path();
        return {folder / std::filesystem::u8path(stem + "_panorama.jpg"), folder / std::filesystem::u8path(stem + "_intensity_panorama.png"), folder / std::filesystem::u8path(stem + "_images")};
    }

    inline std::vector<std::filesystem::path> requestedExportPaths(const std::filesystem::path &_ply,
                                                                   const ExportOptions &_options)
    {
        const auto paths = exportPaths(_ply);
        std::vector<std::filesystem::path> result;
        if (_options.camera_images_)
        {
            result.push_back(paths.images_);
        }
        if (_options.panoramas_)
        {
            result.push_back(paths.intensity_);
            result.push_back(paths.colour_);
        }
        return result;
    }

    inline void requireExportImages(BlkContainer &_container)
    {
        try
        {
            _container.ldrImages();
        }
        catch (const std::exception &error)
        {
            throw std::runtime_error(std::string("Panorama and camera image export need LDR camera images: ") + error.what() + " Turn off the export options and convert again.");
        }
    }

    struct PanoramaSamples
    {
        int width_ = 0;
        int height_ = 0;
        std::vector<float> range_;
        std::vector<std::uint16_t> intensity_;
    };

    inline const char *panoramaConvention()
    {
        return "equirectangular; scanner-local metres, no levelling; u = (pi - atan2(y, x)) / (2 pi) * width; v = (pi / 2 - atan2(z, hypot(x, y))) / pi * height; pixel centres at +0.5; image centre faces +x; top is zenith";
    }

    inline bool panoramaPixel(const std::array<float, 3> &_point,
                              int _width,
                              int _height,
                              int &_u,
                              int &_v)
    {
        const double x = _point[0];
        const double y = _point[1];
        const double z = _point[2];
        const double horizontal = std::hypot(x, y);
        if (horizontal == 0 && z == 0)
        {
            return false;
        }
        const auto u = static_cast<int>(std::floor((0.5 - std::atan2(y, x) / (2 * panoramaPi)) * _width));
        const auto v = static_cast<int>(std::floor((0.5 - std::atan2(z, horizontal) / panoramaPi) * _height));
        _u = u >= _width ? u - _width : std::max(u, 0);
        _v = std::min(std::max(v, 0), _height - 1);
        return true;
    }

    inline std::array<double, 3> panoramaDirection(int _u,
                                                   int _v,
                                                   int _width,
                                                   int _height)
    {
        const double azimuth = 2 * panoramaPi * (0.5 - (_u + 0.5) / _width);
        const double elevation = panoramaPi * (0.5 - (_v + 0.5) / _height);
        return {std::cos(elevation) * std::cos(azimuth), std::cos(elevation) * std::sin(azimuth), std::sin(elevation)};
    }

    inline PanoramaSamples emptyPanorama(int _width,
                                         int _height)
    {
        PanoramaSamples samples;
        samples.width_ = _width;
        samples.height_ = _height;
        samples.range_.assign(static_cast<std::size_t>(_width) * _height, std::numeric_limits<float>::infinity());
        samples.intensity_.assign(samples.range_.size(), 0);
        return samples;
    }

    inline PanoramaSamples splatPanorama(const std::vector<std::array<float, 3>> &_points,
                                         const std::vector<std::uint16_t> &_intensity,
                                         int _width,
                                         int _height)
    {
        auto samples = emptyPanorama(_width, _height);
        for (std::size_t i = 0; i < _points.size(); ++i)
        {
            int u = 0;
            int v = 0;
            if (!panoramaPixel(_points[i], _width, _height, u, v))
            {
                continue;
            }
            const auto &point = _points[i];
            const auto range = std::sqrt(point[0] * point[0] + point[1] * point[1] + point[2] * point[2]);
            const auto index = static_cast<std::size_t>(v) * _width + u;
            if (range < samples.range_[index])
            {
                samples.range_[index] = range;
                samples.intensity_[index] = _intensity[i];
            }
        }
        return samples;
    }

    inline int panoramaFillLevels(int _width)
    {
        int levels = 0;
        while ((1024 << levels) < _width)
        {
            ++levels;
        }
        return levels;
    }

    inline void fillPanoramaHoles(PanoramaSamples &_samples,
                                  int _levels)
    {
        std::vector<PanoramaSamples> pyramid;
        pyramid.reserve(static_cast<std::size_t>(std::max(_levels, 0)));
        const PanoramaSamples *previous = &_samples;
        for (int k = 1; k <= _levels; ++k)
        {
            auto level = emptyPanorama((previous->width_ + 1) / 2, (previous->height_ + 1) / 2);
            for (int v = 0; v < previous->height_; ++v)
            {
                for (int u = 0; u < previous->width_; ++u)
                {
                    const auto source = static_cast<std::size_t>(v) * previous->width_ + u;
                    const auto target = static_cast<std::size_t>(v / 2) * level.width_ + u / 2;
                    if (previous->range_[source] < level.range_[target])
                    {
                        level.range_[target] = previous->range_[source];
                        level.intensity_[target] = previous->intensity_[source];
                    }
                }
            }
            pyramid.push_back(std::move(level));
            previous = &pyramid.back();
        }
        for (int v = 0; v < _samples.height_; ++v)
        {
            for (int u = 0; u < _samples.width_; ++u)
            {
                const auto index = static_cast<std::size_t>(v) * _samples.width_ + u;
                if (std::isfinite(_samples.range_[index]))
                {
                    continue;
                }
                for (int k = 1; k <= _levels; ++k)
                {
                    const auto &level = pyramid[static_cast<std::size_t>(k - 1)];
                    const auto source = static_cast<std::size_t>(v >> k) * level.width_ + (u >> k);
                    if (std::isfinite(level.range_[source]))
                    {
                        _samples.range_[index] = level.range_[source];
                        _samples.intensity_[index] = level.intensity_[source];
                        break;
                    }
                }
            }
        }
    }

    inline std::array<std::uint16_t, 2> intensityRange(const PanoramaSamples &_samples)
    {
        std::vector<std::uint64_t> histogram(65536, 0);
        std::uint64_t count = 0;
        for (std::size_t i = 0; i < _samples.range_.size(); ++i)
        {
            if (std::isfinite(_samples.range_[i]))
            {
                ++histogram[_samples.intensity_[i]];
                ++count;
            }
        }
        if (count == 0)
        {
            return {0, 0};
        }
        const auto percentile = [&histogram, count](double _fraction)
        {
            const auto target = static_cast<std::uint64_t>(std::floor(_fraction * static_cast<double>(count - 1)));
            std::uint64_t seen = 0;
            for (std::size_t value = 0; value < histogram.size(); ++value)
            {
                seen += histogram[value];
                if (seen > target)
                {
                    return static_cast<std::uint16_t>(value);
                }
            }
            return static_cast<std::uint16_t>(65535);
        };
        return {percentile(0.01), percentile(0.99)};
    }

    inline std::vector<std::uint8_t> intensityImage(const PanoramaSamples &_samples,
                                                    const std::array<std::uint16_t, 2> &_range)
    {
        std::vector<std::uint8_t> image(_samples.range_.size(), 0);
        const double low = _range[0];
        const double span = std::max(static_cast<int>(_range[1]) - static_cast<int>(_range[0]), 1);
        for (std::size_t i = 0; i < image.size(); ++i)
        {
            if (std::isfinite(_samples.range_[i]))
            {
                const auto scaled = std::min(std::max((_samples.intensity_[i] - low) * 255.0 / span, 0.0), 255.0);
                image[i] = static_cast<std::uint8_t>(std::lround(scaled));
            }
        }
        return image;
    }

    inline void accumulateCamera(const RgbCamera &_camera,
                                 const PanoramaSamples &_samples,
                                 std::vector<std::uint8_t> &_rgb,
                                 std::vector<float> &_best,
                                 const std::atomic<bool> &_cancel)
    {
        const int width = _samples.width_;
        const int height = _samples.height_;
        std::vector<std::array<double, 2>> azimuths(static_cast<std::size_t>(width));
        std::vector<std::array<double, 2>> elevations(static_cast<std::size_t>(height));
        for (int u = 0; u < width; ++u)
        {
            const double azimuth = 2 * panoramaPi * (0.5 - (u + 0.5) / width);
            azimuths[static_cast<std::size_t>(u)] = {std::cos(azimuth), std::sin(azimuth)};
        }
        for (int v = 0; v < height; ++v)
        {
            const double elevation = panoramaPi * (0.5 - (v + 0.5) / height);
            elevations[static_cast<std::size_t>(v)] = {std::cos(elevation), std::sin(elevation)};
        }
        const auto rows = [&](int _first, int _last)
        {
            for (int v = _first; v < _last && !_cancel.load(); ++v)
            {
                const auto &elevation = elevations[static_cast<std::size_t>(v)];
                for (int u = 0; u < width; ++u)
                {
                    const auto index = static_cast<std::size_t>(v) * width + u;
                    const double range = std::isfinite(_samples.range_[index]) ? _samples.range_[index] : panoramaFarRange;
                    const auto &azimuth = azimuths[static_cast<std::size_t>(u)];
                    const std::array<double, 3> point = {range * elevation[0] * azimuth[0], range * elevation[0] * azimuth[1], range * elevation[1]};
                    double px = 0;
                    double py = 0;
                    double score = 0;
                    if (!projectCamera(_camera, point, px, py, score) || static_cast<float>(score) >= _best[index])
                    {
                        continue;
                    }
                    const auto colour = sampleBilinear(_camera, px, py);
                    std::copy(colour.begin(), colour.end(), _rgb.begin() + static_cast<std::ptrdiff_t>(index * 3));
                    _best[index] = static_cast<float>(score);
                }
            }
        };
        const int count = static_cast<int>(std::max(1u, std::thread::hardware_concurrency()));
        std::vector<std::thread> workers;
        for (int t = 0; t < count; ++t)
        {
            workers.emplace_back(rows, height * t / count, height * (t + 1) / count);
        }
        for (auto &worker : workers)
        {
            worker.join();
        }
        checkCancel(_cancel);
    }

    inline nlohmann::json cameraRecord(const std::string &_name,
                                       const RgbCamera &_camera)
    {
        const auto first = [&_camera](const std::string &_key)
        {
            const auto entry = _camera.metadata.find(_key);
            return entry == _camera.metadata.end() || entry->second.empty() ? nlohmann::json() : nlohmann::json(entry->second.front());
        };
        const auto gains = nlohmann::json::array({first("GainR"), first("GainG"), first("GainB")});
        const bool complete = std::none_of(gains.begin(), gains.end(), [](const nlohmann::json &_value)
        {
            return _value.is_null();
        });
        return {{"name", _name},
                {"file", _name + ".png"},
                {"width", _camera.width},
                {"height", _camera.height},
                {"fx", _camera.fx},
                {"fy", _camera.fy},
                {"cx", _camera.cx},
                {"cy", _camera.cy},
                {"rotation", _camera.rotation},
                {"translation", _camera.translation},
                {"exposure_time", first("ExposureTime")},
                {"gain_rgb", complete ? gains : nlohmann::json()},
                {"raw", _camera.metadata}};
    }

    inline std::vector<std::string> orderedImageNames(const std::map<std::string, Block> &_blocks)
    {
        std::vector<std::string> names;
        for (const auto &entry : _blocks)
        {
            names.push_back(entry.first);
        }
        const auto number = [](const std::string &_name)
        {
            return std::stoull(_name.substr(_name.find_last_not_of("0123456789") + 1));
        };
        std::sort(names.begin(), names.end(), [&number](const std::string &_left, const std::string &_right)
        {
            return number(_left) < number(_right);
        });
        return names;
    }

    inline std::pair<std::vector<std::array<float, 3>>, std::vector<std::uint16_t>> readIntermediateCloud(const std::filesystem::path &_ply,
                                                                                                        std::uint64_t _count,
                                                                                                        const std::atomic<bool> &_cancel)
    {
        std::ifstream stream(_ply, std::ios::binary);
        std::string line;
        while (std::getline(stream, line) && line != "end_header")
        {
        }
        if (!stream)
        {
            throw std::runtime_error("Missing intermediate PLY header");
        }
        std::vector<std::array<float, 3>> points(_count);
        std::vector<std::uint16_t> intensity(_count);
        std::array<char, 14> record{};
        for (std::uint64_t i = 0; i < _count; ++i)
        {
            if (i % 65536 == 0)
            {
                checkCancel(_cancel);
            }
            stream.read(record.data(), static_cast<std::streamsize>(record.size()));
            if (!stream)
            {
                throw std::runtime_error("Incomplete intermediate PLY");
            }
            std::memcpy(points[i].data(), record.data(), 12);
            std::memcpy(&intensity[i], record.data() + 12, 2);
        }
        return {std::move(points), std::move(intensity)};
    }

    inline nlohmann::json writePanoramas(BlkContainer &_container,
                                         const std::filesystem::path &_cloud,
                                         std::uint64_t _count,
                                         int _width,
                                         const std::filesystem::path &_colour,
                                         const std::filesystem::path &_intensity,
                                         const std::atomic<bool> &_cancel,
                                         const std::function<void(double)> &_progress)
    {
        const int height = _width / 2;
        _progress(0);
        PanoramaSamples samples;
        {
            const auto cloud = readIntermediateCloud(_cloud, _count, _cancel);
            samples = splatPanorama(cloud.first, cloud.second, _width, height);
        }
        checkCancel(_cancel);
        fillPanoramaHoles(samples, panoramaFillLevels(_width));
        _progress(.1);
        const auto range = intensityRange(samples);
        writePng(_intensity, _width, height, 1, intensityImage(samples, range).data());
        _progress(.2);
        std::vector<std::uint8_t> rgb(samples.range_.size() * 3, 0);
        std::vector<float> best(samples.range_.size(), std::numeric_limits<float>::infinity());
        const auto blocks = _container.ldrImages();
        const auto names = orderedImageNames(blocks);
        for (std::size_t i = 0; i < names.size(); ++i)
        {
            const auto &block = blocks.at(names[i]);
            auto bytes = _container.read(block.offset_, block.size_);
            accumulateCamera(readRgbCamera(bytes), samples, rgb, best, _cancel);
            _progress(.2 + .7 * static_cast<double>(i + 1) / static_cast<double>(names.size()));
        }
        checkCancel(_cancel);
        writeJpeg(_colour, _width, height, rgb.data(), 92);
        const auto coloured = std::count_if(best.begin(), best.end(), [](float _value)
        {
            return std::isfinite(_value);
        });
        _progress(1);
        return {{"width", _width},
                {"height", height},
                {"convention", panoramaConvention()},
                {"coloured_fraction", static_cast<double>(coloured) / static_cast<double>(best.size())},
                {"intensity_range", range}};
    }

    inline nlohmann::json writeCameraImages(BlkContainer &_container,
                                            const std::filesystem::path &_directory,
                                            const std::string &_source_name,
                                            const std::string &_source_sha256,
                                            const std::atomic<bool> &_cancel,
                                            const std::function<void(double)> &_progress)
    {
        std::filesystem::create_directories(_directory);
        const auto blocks = _container.ldrImages();
        const auto names = orderedImageNames(blocks);
        auto images = nlohmann::json::array();
        _progress(0);
        for (std::size_t i = 0; i < names.size(); ++i)
        {
            checkCancel(_cancel);
            const auto &block = blocks.at(names[i]);
            auto bytes = _container.read(block.offset_, block.size_);
            const auto camera = readRgbCamera(bytes);
            writePng(_directory / std::filesystem::u8path(names[i] + ".png"), camera.width, camera.height, 3, camera.pixels.data());
            images.push_back(cameraRecord(names[i], camera));
            _progress(static_cast<double>(i + 1) / static_cast<double>(names.size()));
        }
        const nlohmann::json document = {{"schema_version", 1},
                                         {"source_name", _source_name},
                                         {"source_sha256", _source_sha256},
                                         {"convention", "q = R*p_scanner + t; u = fx*qx/qz + cx; v = fy*qy/qz + cy; metres; scanner-local frame"},
                                         {"independently_validated", false},
                                         {"images", images}};
        writeText(_directory / "cameras.json", document.dump(2) + "\n");
        return {{"image_count", names.size()}};
    }

    inline std::string publishExports(const std::filesystem::path &_work,
                                      const ExportPaths &_targets,
                                      const ExportOptions &_options)
    {
        std::vector<std::pair<std::filesystem::path, std::filesystem::path>> moves;
        if (_options.camera_images_)
        {
            moves.emplace_back(_work / "images", _targets.images_);
        }
        if (_options.panoramas_)
        {
            moves.emplace_back(_work / "intensity_panorama.png", _targets.intensity_);
            moves.emplace_back(_work / "panorama.jpg", _targets.colour_);
        }
        std::string published;
        for (const auto &move : moves)
        {
            try
            {
                if (std::filesystem::is_directory(move.first))
                {
                    publishDirectory(move.first, move.second);
                }
                else
                {
                    publishFile(move.first, move.second);
                }
            }
            catch (const std::exception &error)
            {
                throw std::runtime_error("Cannot publish " + move.second.u8string() + ": " + error.what() + (published.empty() ? "" : ". Already published: " + published));
            }
            published += (published.empty() ? "" : ", ") + move.second.u8string();
        }
        return published;
    }

    inline void publishConversion(const std::filesystem::path &_work,
                                  const std::filesystem::path &_destination,
                                  const ExportOptions &_options)
    {
        auto outputs = requestedExportPaths(_destination, _options);
        outputs.push_back(_destination);
        for (const auto &path : outputs)
        {
            if (std::filesystem::exists(path))
            {
                throw std::runtime_error("Output already exists: " + path.u8string());
            }
        }
        const auto published = publishExports(_work, exportPaths(_destination), _options);
        try
        {
            publishFile(_work / "cloud.ply", _destination);
        }
        catch (const std::exception &error)
        {
            throw std::runtime_error("Cannot publish " + _destination.u8string() + ": " + error.what() + ". Already published: " + published);
        }
    }

    inline std::filesystem::path exportFilePath(const nlohmann::json &_job,
                                                const std::string &_item)
    {
        if (_job.value("status", "") != "completed")
        {
            throw std::runtime_error("Scan has not completed conversion");
        }
        const auto result = _job.find("result");
        if (result == _job.end() || !result->is_object() || !result->contains("panorama") || !result->at("panorama").is_object())
        {
            throw std::runtime_error("No images were exported for this scan");
        }
        const auto &panorama = result->at("panorama");
        const auto stored = [&panorama](const std::string &_key)
        {
            if (!panorama.contains(_key) || !panorama.at(_key).is_string())
            {
                throw std::runtime_error("This image was not exported for this scan");
            }
            return std::filesystem::u8path(panorama.at(_key).get<std::string>());
        };
        if (_item == "colour")
        {
            return stored("colour_path");
        }
        if (_item == "intensity")
        {
            return stored("intensity_path");
        }
        if (_item == "cameras.json")
        {
            return stored("images_path") / "cameras.json";
        }
        const std::string prefix = "LDR_Image_";
        const std::string suffix = ".png";
        const bool camera = _item.size() > prefix.size() + suffix.size() && _item.compare(0, prefix.size(), prefix) == 0 && _item.compare(_item.size() - suffix.size(), suffix.size(), suffix) == 0 && _item.find_first_not_of("0123456789", prefix.size()) == _item.size() - suffix.size();
        if (!camera)
        {
            throw std::runtime_error("Unknown image: " + _item);
        }
        return stored("images_path") / std::filesystem::u8path(_item);
    }
} //namespace blk_core
