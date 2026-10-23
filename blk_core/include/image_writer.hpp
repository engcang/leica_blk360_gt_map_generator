#pragma once

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>

#define STB_IMAGE_WRITE_STATIC
#define STBI_WRITE_NO_STDIO
#define STB_IMAGE_WRITE_IMPLEMENTATION
#include <stb_image_write.h>

namespace blk_core
{
    inline void appendImageBytes(void *_context,
                                 void *_data,
                                 int _size)
    {
        static_cast<std::ofstream *>(_context)->write(static_cast<const char *>(_data), _size);
    }

    inline std::ofstream openImage(const std::filesystem::path &_path)
    {
        std::ofstream stream(_path, std::ios::binary);
        if (!stream)
        {
            throw std::runtime_error("Cannot write image: " + _path.u8string());
        }
        return stream;
    }

    inline void finishImage(std::ofstream &_stream,
                            int _result,
                            const std::filesystem::path &_path)
    {
        _stream.close();
        if (!_result || !_stream)
        {
            throw std::runtime_error("Cannot write image: " + _path.u8string());
        }
    }

    inline void writePng(const std::filesystem::path &_path,
                         int _width,
                         int _height,
                         int _channels,
                         const std::uint8_t *_pixels)
    {
        auto stream = openImage(_path);
        const auto result = stbi_write_png_to_func(appendImageBytes, &stream, _width, _height, _channels, _pixels, _width * _channels);
        finishImage(stream, result, _path);
    }

    inline void writeJpeg(const std::filesystem::path &_path,
                          int _width,
                          int _height,
                          const std::uint8_t *_rgb,
                          int _quality)
    {
        auto stream = openImage(_path);
        const auto result = stbi_write_jpg_to_func(appendImageBytes, &stream, _width, _height, 3, _rgb, _quality);
        finishImage(stream, result, _path);
    }
} //namespace blk_core
