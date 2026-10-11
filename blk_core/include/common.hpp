#pragma once

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iterator>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include <json.hpp>

namespace blk_core
{
    struct Progress
    {
        std::string stage_;
        double fraction_ = 0;
    };

    class Cancelled: public std::runtime_error
    {
    public:
        Cancelled():
            std::runtime_error("Conversion cancelled")
        {
        }
    };

    inline void checkCancel(const std::atomic<bool> &_cancel)
    {
        if (_cancel.load())
        {
            throw Cancelled();
        }
    }

    inline std::string newId()
    {
        std::random_device random;
        std::ostringstream value;
        value << std::hex << std::setfill('0');
        for (int i = 0; i < 4; ++i)
        {
            value << std::setw(8) << static_cast<std::uint32_t>(random());
        }
        return value.str();
    }

    template<typename T>
    inline T readNumber(const std::vector<std::uint8_t> &_bytes,
                        std::size_t _offset)
    {
        if (_offset > _bytes.size() || sizeof(T) > _bytes.size() - _offset)
        {
            throw std::runtime_error("Truncated numeric field");
        }
        T value;
        std::memcpy(&value, _bytes.data() + _offset, sizeof(T));
        return value;
    }

    inline void requireLittleEndian()
    {
        std::uint16_t value = 1;
        if (*reinterpret_cast<std::uint8_t *>(&value) != 1)
        {
            throw std::runtime_error("This release requires a little-endian CPU");
        }
    }

    inline std::vector<std::uint8_t> readBytes(std::istream &_stream,
                                               std::size_t _count)
    {
        std::vector<std::uint8_t> result(_count);
        if (!_stream.read(reinterpret_cast<char *>(result.data()), static_cast<std::streamsize>(_count)))
        {
            throw std::runtime_error("Unexpected end of input");
        }
        return result;
    }

    inline std::string readText(const std::filesystem::path &_path)
    {
        std::ifstream stream(_path, std::ios::binary);
        if (!stream)
        {
            throw std::runtime_error("Cannot open " + _path.u8string());
        }
        return std::string(std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>());
    }

    inline void writeText(const std::filesystem::path &_path,
                          const std::string &_text)
    {
        std::ofstream stream(_path, std::ios::binary);
        stream.write(_text.data(), static_cast<std::streamsize>(_text.size()));
        stream.close();
        if (!stream)
        {
            throw std::runtime_error("Cannot write " + _path.u8string());
        }
    }

    inline void replaceAll(std::string &_text,
                           const std::string &_from,
                           const std::string &_to)
    {
        std::size_t pos = 0;
        while ((pos = _text.find(_from, pos)) != std::string::npos)
        {
            _text.replace(pos, _from.size(), _to);
            pos += _to.size();
        }
    }

    inline std::string htmlEscape(std::string _text)
    {
        replaceAll(_text, "&", "&amp;");
        replaceAll(_text, "<", "&lt;");
        replaceAll(_text, ">", "&gt;");
        replaceAll(_text, "\"", "&quot;");
        return _text;
    }

    inline std::string base64(const std::vector<std::uint8_t> &_bytes)
    {
        const std::string alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        std::string result;
        result.reserve((_bytes.size() + 2) / 3 * 4);
        for (std::size_t i = 0; i < _bytes.size(); i += 3)
        {
            std::uint32_t value = static_cast<std::uint32_t>(_bytes[i]) << 16;
            if (i + 1 < _bytes.size())
            {
                value |= static_cast<std::uint32_t>(_bytes[i + 1]) << 8;
            }
            if (i + 2 < _bytes.size())
            {
                value |= _bytes[i + 2];
            }
            result += alphabet[(value >> 18) & 63];
            result += alphabet[(value >> 12) & 63];
            result += i + 1 < _bytes.size() ? alphabet[(value >> 6) & 63] : '=';
            result += i + 2 < _bytes.size() ? alphabet[value & 63] : '=';
        }
        return result;
    }

    struct Point
    {
        float x_ = 0;
        float y_ = 0;
        float z_ = 0;
        std::uint16_t intensity_ = 0;
    };
} //namespace blk_core
