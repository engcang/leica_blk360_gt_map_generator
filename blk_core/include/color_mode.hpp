#pragma once
#include <cstddef>
namespace blk_core
{
#ifdef BLK_RGB
    inline constexpr bool rgbEdition = true;
#else
    inline constexpr bool rgbEdition = false;
#endif
    inline constexpr std::size_t plyStride = rgbEdition ? 15 : 14;
    inline const char *colourProperties()
    {
        return rgbEdition ? "property uchar red\nproperty uchar green\nproperty uchar blue\n" : "property ushort intensity\n";
    }
} //namespace blk_core
