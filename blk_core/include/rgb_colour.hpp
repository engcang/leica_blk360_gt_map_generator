#pragma once
#include "blk_container.hpp"
#include "jpeg_xr.hpp"
#include <atomic>
#include <functional>
#include <limits>

namespace blk_core
{
    struct RgbCamera
    {
        std::array<double, 9> rotation{};
        std::array<double, 3> translation{};
        double fx = 0, fy = 0, cx = 0, cy = 0;
        int width = 0, height = 0;
        std::vector<std::uint8_t> pixels;
        std::map<std::string, std::vector<double>> metadata;
    };
    inline std::map<std::string, std::vector<double>> readCameraMetadata(const std::vector<std::uint8_t> &_bytes)
    {
        if (_bytes.size() < 18 || std::memcmp(_bytes.data() + _bytes.size() - 8, "HXMETAV1", 8))
            throw std::runtime_error("Missing LDR camera metadata");
        const auto end = _bytes.size() - 16;
        auto cursor = readNumber<std::uint64_t>(_bytes, end);
        if (cursor > end || end - cursor < 2)
            throw std::runtime_error("Invalid image metadata bounds");
        const auto count = readNumber<std::uint16_t>(_bytes, cursor);
        cursor += 2;
        std::map<std::string, std::vector<double>> fields;
        for (unsigned i = 0; i < count; ++i)
        {
            if (cursor + 2 > end)
                throw std::runtime_error("Truncated image metadata");
            const auto n = readNumber<std::uint16_t>(_bytes, cursor);
            cursor += 2;
            if (!n || cursor + n + 5 > end || _bytes[cursor + n - 1])
                throw std::runtime_error("Invalid image property");
            const std::string name(reinterpret_cast<const char *>(_bytes.data() + cursor), n - 1);
            cursor += n;
            const auto length = readNumber<std::uint16_t>(_bytes, cursor);
            const auto type = _bytes[cursor + 4];
            cursor += 5;
            if (cursor + length > end)
                throw std::runtime_error("Truncated image property");
            if (type == 11)
            {
                if (length % 8)
                    throw std::runtime_error("Invalid camera coefficient size");
                auto &values = fields[name];
                for (std::size_t j = 0; j < length; j += 8)
                {
                    const auto value = readNumber<double>(_bytes, cursor + j);
                    if (!std::isfinite(value))
                        throw std::runtime_error("Non-finite camera coefficient");
                    values.push_back(value);
                }
            }
            else if (type == 10 && length && length % 4 == 0)
            {
                std::vector<double> values;
                for (std::size_t j = 0; j < length; j += 4)
                    values.push_back(readNumber<float>(_bytes, cursor + j));
                if (std::all_of(values.begin(), values.end(), [](double _value)
                {
                    return std::isfinite(_value);
                }))
                    fields[name] = values;
            }
            cursor += length;
        }
        return fields;
    }
    inline RgbCamera readRgbCamera(std::vector<std::uint8_t> &bytes)
    {
        auto fields = readCameraMetadata(bytes);
        if (fields["Rotation"].size() != 9 || fields["Translation"].size() != 3 || fields["CameraModel"].size() != 60)
            throw std::runtime_error("Unsupported LDR camera calibration");
        RgbCamera camera;
        std::copy_n(fields["Rotation"].begin(), 9, camera.rotation.begin());
        std::copy_n(fields["Translation"].begin(), 3, camera.translation.begin());
        const auto &m = fields["CameraModel"];
        if (m[0] != 1 || m[1] != 0 || m[2] != 0 || m[18] != 0 || m[20] != 0 ||
            std::any_of(m.begin() + 23, m.end(), [](double x)
        {
            return x != 0;
        }))
            throw std::runtime_error("Unsupported camera model or distortion; refusing inaccurate colour projection");
        camera.fx = m[17];
        camera.fy = m[21];
        camera.cx = m[19];
        camera.cy = m[22];
        if (camera.fx <= 0 || camera.fy <= 0)
            throw std::runtime_error("Invalid camera focal length");
        JpegXrDecoder decoder;
        camera.pixels = decoder.decode(bytes, 24, camera.width, 0, &camera.height);
        if (m[3] != camera.width || m[4] != camera.height)
            throw std::runtime_error("Image/calibration dimensions differ");
        camera.metadata = std::move(fields);
        return camera;
    }
    inline bool projectRgb(const RgbCamera &camera, const std::array<float, 3> &p, int &u, int &v, float &depth, float &score)
    {
        std::array<double, 3> q = camera.translation;
        for (int r = 0; r < 3; ++r)
            for (int k = 0; k < 3; ++k)
                q[r] += camera.rotation[r * 3 + k] * p[k];
        if (q[2] <= .05)
            return false;
        const double x = q[0] / q[2], y = q[1] / q[2];
        const double px = camera.fx * x + camera.cx, py = camera.fy * y + camera.cy;
        if (px < 1 || py < 1 || px >= camera.width - 2 || py >= camera.height - 2)
            return false;
        u = static_cast<int>(px);
        v = static_cast<int>(py);
        depth = static_cast<float>(q[2]);
        score = static_cast<float>(x * x + y * y);
        return true;
    }
    inline bool projectCamera(const RgbCamera &_camera, const std::array<double, 3> &_point, double &_px, double &_py, double &_score)
    {
        std::array<double, 3> q = _camera.translation;
        for (int r = 0; r < 3; ++r)
            for (int k = 0; k < 3; ++k)
                q[r] += _camera.rotation[r * 3 + k] * _point[k];
        if (q[2] <= .05)
            return false;
        const double x = q[0] / q[2], y = q[1] / q[2];
        _px = _camera.fx * x + _camera.cx;
        _py = _camera.fy * y + _camera.cy;
        if (_px < 1 || _py < 1 || _px >= _camera.width - 2 || _py >= _camera.height - 2)
            return false;
        _score = x * x + y * y;
        return true;
    }
    inline std::array<std::uint8_t, 3> sampleBilinear(const RgbCamera &_camera, double _px, double _py)
    {
        const double x = std::min(std::max(_px - .5, 0.0), _camera.width - 1.0);
        const double y = std::min(std::max(_py - .5, 0.0), _camera.height - 1.0);
        const int x0 = static_cast<int>(x), y0 = static_cast<int>(y);
        const int x1 = std::min(x0 + 1, _camera.width - 1), y1 = std::min(y0 + 1, _camera.height - 1);
        const double wx = x - x0, wy = y - y0;
        std::array<std::uint8_t, 3> result{};
        for (std::size_t c = 0; c < 3; ++c)
        {
            const auto at = [&_camera, c](int _x, int _y)
            {
                return static_cast<double>(_camera.pixels[(static_cast<std::size_t>(_y) * _camera.width + _x) * 3 + c]);
            };
            const double top = at(x0, y0) * (1 - wx) + at(x1, y0) * wx;
            const double bottom = at(x0, y1) * (1 - wx) + at(x1, y1) * wx;
            result[c] = static_cast<std::uint8_t>(std::lround(top * (1 - wy) + bottom * wy));
        }
        return result;
    }
    inline nlohmann::json colourPly(BlkContainer &container, const std::filesystem::path &input, const std::filesystem::path &output, std::uint64_t count, const std::atomic<bool> &cancel, const std::function<void(const Progress &)> &progress)
    {
        const auto blocks = container.ldrImages();
        std::ifstream source(input, std::ios::binary);
        std::string line;
        while (std::getline(source, line) && line != "end_header")
        {
        }
        if (!source)
            throw std::runtime_error("Missing intermediate PLY header");
        std::vector<std::array<float, 3>> xyz(count);
        for (std::size_t i = 0; i < count; ++i)
        {
            if (i % 65536 == 0)
                checkCancel(cancel);
            std::array<char, 14> point{};
            source.read(point.data(), 14);
            if (!source)
                throw std::runtime_error("Incomplete intermediate PLY");
            std::memcpy(xyz[i].data(), point.data(), 12);
        }
        source.close();
        std::vector<std::array<unsigned char, 3>> rgb(count, {128, 128, 128});
        std::vector<float> best(count, INFINITY);
        std::size_t image = 0;
        for (const auto &entry : blocks)
        {
            checkCancel(cancel);
            progress({"Colouring " + entry.first, .55 + .38 * image++ / blocks.size()});
            auto bytes = container.read(entry.second.offset_, entry.second.size_);
            const auto camera = readRgbCamera(bytes);
            const auto width = (camera.width + 3) / 4;
            std::vector<float> depthBuffer(width * ((camera.height + 3) / 4), INFINITY);
            for (int pass = 0; pass < 2; ++pass)
                for (std::size_t i = 0; i < count; ++i)
                {
                    if (i % 65536 == 0)
                        checkCancel(cancel);
                    int u, v;
                    float depth, score;
                    if (!projectRgb(camera, xyz[i], u, v, depth, score))
                        continue;
                    auto &nearest = depthBuffer[(v / 4) * width + u / 4];
                    if (!pass)
                        nearest = std::min(nearest, depth);
                    else if (score < best[i] && depth <= nearest + std::max(.02f, depth * .005f))
                    {
                        const auto pixel = (static_cast<std::size_t>(v) * camera.width + u) * 3;
                        std::copy_n(camera.pixels.data() + pixel, 3, rgb[i].data());
                        best[i] = score;
                    }
                }
        }
        std::ofstream result(output, std::ios::binary);
        result.exceptions(std::ios::failbit | std::ios::badbit);
        result << "ply\nformat binary_little_endian 1.0\ncomment Experimental LDR projection; uncoloured points are gray\nelement vertex " << count
               << "\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n";
        std::uint64_t coloured = 0;
        for (std::size_t i = 0; i < count; ++i)
        {
            if (i % 65536 == 0)
                checkCancel(cancel);
            result.write(reinterpret_cast<const char *>(xyz[i].data()), 12);
            result.write(reinterpret_cast<const char *>(rgb[i].data()), 3);
            if (std::isfinite(best[i]))
                ++coloured;
        }
        result.close();
        return {{"colour_mode", "rgb"}, {"ldr_images", blocks.size()}, {"coloured_points", coloured}, {"uncoloured_points", count - coloured}, {"uncoloured_rgb", {128, 128, 128}}, {"colour_independently_validated", false}};
    }
} //namespace blk_core
