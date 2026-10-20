#include "rgb_colour.hpp"
#include "placement.hpp"
#include "platform.hpp"
#include <iostream>
int main()
{
    try
    {
        blk_core::RgbCamera c;
        c.rotation = {1, 0, 0, 0, 1, 0, 0, 0, 1};
        c.fx = c.fy = 100;
        c.cx = c.cy = 50;
        c.width = c.height = 100;
        int u, v;
        float z, score;
        if (!blk_core::projectRgb(c, {.2f, .1f, 2}, u, v, z, score) || u != 60 || v != 55 || z != 2)
            throw std::runtime_error("RGB projection failed");
        if (blk_core::projectRgb(c, {0, 0, -1}, u, v, z, score))
            throw std::runtime_error("Behind-camera point accepted");
        std::vector<std::uint8_t> invalid(20);
        bool rejected = false;
        try
        {
            blk_core::readRgbCamera(invalid);
        }
        catch (const std::exception &)
        {
            rejected = true;
        }
        if (!rejected)
            throw std::runtime_error("Malformed metadata accepted");
        const auto root = std::filesystem::temp_directory_path() / ("blk-rgb-test-" + blk_core::newId());
        std::filesystem::create_directory(root);
        const auto path = root / "rgb.ply";
        {
            std::ofstream out(path, std::ios::binary);
            out << "ply\nformat binary_little_endian 1.0\nelement vertex 1\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n";
            const float xyz[3] = {1, 2, 3};
            const unsigned char rgb[3] = {255, 12, 129};
            out.write(reinterpret_cast<const char *>(xyz), 12);
            out.write(reinterpret_cast<const char *>(rgb), 3);
        }
        const auto bytes = blk_core::placementSample(path, 0, 1);
        float sample[4];
        std::memcpy(sample, bytes.data(), 16);
        if (sample[0] != 1 || sample[1] != 2 || sample[2] != 3 || sample[3] != 16714881.f)
            throw std::runtime_error("RGB placement sample failed");
        std::filesystem::remove(path);
        std::filesystem::remove(root);
        std::cout << "RGB tests passed\n";
        return 0;
    }
    catch (const std::exception &e)
    {
        std::cerr << e.what() << "\n";
        return 1;
    }
}
