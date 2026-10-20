#pragma once

#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

#ifndef __in_ecount
#define __in_ecount(size)
#endif
#ifndef __out_ecount
#define __out_ecount(size)
#endif
#ifndef __in
#define __in
#endif
#ifndef __out
#define __out
#endif
#include <JXRGlue.h>
#undef min
#undef max
#undef __in
#undef __out

#include "common.hpp"

namespace blk_core
{
    class JpegXrDecoder
    {
    public:
        JpegXrDecoder() = default;
        JpegXrDecoder(const JpegXrDecoder &) = delete;
        JpegXrDecoder &operator=(const JpegXrDecoder &) = delete;
        ~JpegXrDecoder()
        {
            if (decoder_)
            {
                decoder_->Release(&decoder_);
            }
            if (stream_)
            {
                stream_->Close(&stream_);
            }
        }

        std::vector<std::uint8_t> decode(std::vector<std::uint8_t> &_encoded,
                                         int _bits,
                                         int &_rows,
                                         int _columns, int *_height = nullptr)
        {
            check(CreateWS_Memory(&stream_, _encoded.data(), _encoded.size()), "memory stream");
            check(PKImageDecode_Create_WMP(&decoder_), "decoder");
            check(decoder_->Initialize(decoder_, stream_), "JPEG XR header");
            PKPixelFormatGUID format;
            check(decoder_->GetPixelFormat(decoder_, &format), "pixel format");
            const auto &expected = _bits == 24 ? GUID_PKPixelFormat24bppRGB : (_bits == 32 ? GUID_PKPixelFormat32bppGrayFixedPoint : GUID_PKPixelFormat16bppGray);
            if (std::memcmp(&format, &expected, sizeof(format)) != 0)
            {
                throw std::runtime_error("Unsupported JPEG XR integer format");
            }
            I32 width = 0;
            I32 height = 0;
            check(decoder_->GetSize(decoder_, &width, &height), "image size");
            if (width <= 0 || width > 100000 || height <= 0 || height > 100000 || (_columns > 0 && height != _columns) || (_rows != 0 && _rows != width))
            {
                throw std::runtime_error("Inconsistent JPEG XR plane dimensions");
            }
            _rows = width;
            if (_height) *_height = height;
            const auto stride = static_cast<std::size_t>(width) * (_bits / 8);
            const auto bytes = stride * static_cast<std::size_t>(height);
            if (bytes > 2147483647)
            {
                throw std::runtime_error("Scan plane exceeds supported 2 GiB limit");
            }
            PKPixelInfo info = {};
            info.pGUIDPixFmt = &format;
            check(PixelFormatLookup(&info, LOOKUP_FORWARD), "pixel layout");
            //Codex: Preserve integer fixed-point samples; never convert them to floating-point image colours.
            decoder_->WMP.wmiI.cfColorFormat = info.cfColorFormat;
            decoder_->WMP.wmiI.bdBitDepth = info.bdBitDepth;
            decoder_->WMP.wmiI.cBitsPerUnit = info.cbitUnit;
            decoder_->WMP.wmiI.cThumbnailWidth = width;
            decoder_->WMP.wmiI.cThumbnailHeight = height;
            decoder_->WMP.wmiI.cROIWidth = width;
            decoder_->WMP.wmiI.cROIHeight = height;
            decoder_->WMP.wmiI.bSkipFlexbits = FALSE;
            decoder_->WMP.wmiSCP.uAlphaMode = 0;
            decoder_->WMP.wmiSCP.sbSubband = SB_ALL;
            PKRect rectangle = {0, 0, width, height};
            std::vector<std::uint8_t> result(bytes);
            check(decoder_->Copy(decoder_, &rectangle, result.data(), static_cast<U32>(stride)), "JPEG XR decompression");
            return result;
        }

    private:
        static void check(ERR _result,
                          const std::string &_stage)
        {
            if (_result != WMP_errSuccess)
            {
                throw std::runtime_error(_stage + " failed: " + std::to_string(_result));
            }
        }
        PKImageDecode *decoder_ = nullptr;
        WMPStream *stream_ = nullptr;
    };
} //namespace blk_core
