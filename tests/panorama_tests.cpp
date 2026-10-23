#include <array>
#include <atomic>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <limits>
#include <map>
#include <string>
#include <vector>

#include "project.hpp"
#include "common.hpp"
#include "converter.hpp"
#include "image_writer.hpp"
#include "panorama.hpp"
#include "platform.hpp"

void require(bool _value,
             const std::string &_message)
{
    if (!_value)
    {
        throw std::runtime_error(_message);
    }
}

void expectError(const std::function<void()> &_function)
{
    bool failed = false;
    try
    {
        _function();
    }
    catch (const std::exception &)
    {
        failed = true;
    }
    require(failed, "Expected invalid input to be rejected");
}

std::string readPrefix(const std::filesystem::path &_path,
                       std::size_t _count)
{
    std::ifstream stream(_path, std::ios::binary);
    std::string bytes(_count, '\0');
    stream.read(&bytes[0], static_cast<std::streamsize>(_count));
    require(stream.gcount() == static_cast<std::streamsize>(_count), "Image file is too short");
    return bytes;
}

void testImageWriters(const std::filesystem::path &_root)
{
    const std::vector<std::uint8_t> pixels = {255, 0, 0, 0, 0, 255};
    blk_core::writePng(_root / "a.png", 2, 1, 3, pixels.data());
    require(readPrefix(_root / "a.png", 8) == std::string("\x89PNG\r\n\x1a\n", 8), "PNG signature missing");
    blk_core::writeJpeg(_root / "a.jpg", 2, 1, pixels.data(), 92);
    require(readPrefix(_root / "a.jpg", 2) == std::string("\xFF\xD8", 2), "JPEG signature missing");
    const auto unicode = _root / std::filesystem::u8path("\xED\x8C\x8C\xEB\x85\xB8\xEB\x9D\xBC\xEB\xA7\x88 test.png");
    blk_core::writePng(unicode, 2, 1, 3, pixels.data());
    require(std::filesystem::is_regular_file(unicode), "Non-ASCII image path failed");
}

void testPublishDirectory(const std::filesystem::path &_root)
{
    const auto source = _root / "src";
    std::filesystem::create_directory(source);
    blk_core::writeText(source / "x.txt", "data");
    blk_core::publishDirectory(source, _root / "dst");
    require(blk_core::readText(_root / "dst" / "x.txt") == "data", "Published directory content differs");
    require(!std::filesystem::exists(source), "Published source directory remains");
    const auto kept = _root / "src2";
    const auto existing = _root / "dst2";
    std::filesystem::create_directory(kept);
    blk_core::writeText(kept / "x.txt", "new");
    std::filesystem::create_directory(existing);
    expectError([&kept, &existing]
    {
        blk_core::publishDirectory(kept, existing);
    });
    require(blk_core::readText(kept / "x.txt") == "new", "Refused publish lost its source");
    require(std::filesystem::is_empty(existing), "Existing directory was modified");
}

void requirePixel(const std::array<float, 3> &_point,
                  int _u,
                  int _v)
{
    int u = -1;
    int v = -1;
    require(blk_core::panoramaPixel(_point, 8, 4, u, v) && u == _u && v == _v, "Panorama pixel mapping failed");
}

void testPanoramaPixel()
{
    requirePixel({1, 0, 0}, 4, 2);
    requirePixel({0, 1, 0}, 2, 2);
    requirePixel({0, -1, 0}, 6, 2);
    requirePixel({-1, 0, 0}, 0, 2);
    requirePixel({-1, -1e-6f, 0}, 7, 2);
    requirePixel({0, 0, 1}, 4, 0);
    requirePixel({0, 0, -1}, 4, 3);
    int u = 0;
    int v = 0;
    require(!blk_core::panoramaPixel({0, 0, 0}, 8, 4, u, v), "Scanner origin was mapped");
}

void testPanoramaRoundTrip()
{
    for (int v = 0; v < 4; ++v)
    {
        for (int u = 0; u < 8; ++u)
        {
            const auto direction = blk_core::panoramaDirection(u, v, 8, 4);
            const std::array<float, 3> point = {static_cast<float>(direction[0]), static_cast<float>(direction[1]), static_cast<float>(direction[2])};
            requirePixel(point, u, v);
        }
    }
}

void testSplatNearest()
{
    const std::vector<std::array<float, 3>> points = {{2, 0, 0}, {1, 0, 0}};
    const std::vector<std::uint16_t> intensity = {10, 20};
    const auto samples = blk_core::splatPanorama(points, intensity, 8, 4);
    const std::size_t index = 2 * 8 + 4;
    require(samples.range_[index] == 1 && samples.intensity_[index] == 20, "Splat did not keep the nearest point");
    require(std::isinf(samples.range_[0]), "Unsampled pixel was filled by the splat");
}

void testFillHoles()
{
    auto single = blk_core::emptyPanorama(16, 8);
    single.range_[0] = 2;
    single.intensity_[0] = 7;
    auto wide = single;
    blk_core::fillPanoramaHoles(single, 1);
    require(single.range_[1] == 2 && single.range_[16] == 2 && single.range_[17] == 2 && single.intensity_[17] == 7, "Neighbouring holes were not filled");
    require(std::isinf(single.range_[2 * 16 + 2]), "One-level fill reached too far");
    blk_core::fillPanoramaHoles(wide, 2);
    require(wide.range_[3 * 16 + 3] == 2 && std::isinf(wide.range_[4 * 16 + 4]), "Two-level fill reach is wrong");
    auto nearest = blk_core::emptyPanorama(16, 8);
    nearest.range_[0] = 5;
    nearest.range_[1] = 3;
    blk_core::fillPanoramaHoles(nearest, 1);
    require(nearest.range_[16] == 3, "Hole was not filled from the nearest surface");
    require(blk_core::panoramaFillLevels(4096) == 2 && blk_core::panoramaFillLevels(8192) == 3 && blk_core::panoramaFillLevels(16384) == 4, "Fill levels do not follow the width");
}

void testIntensityImage()
{
    auto samples = blk_core::emptyPanorama(128, 1);
    for (int i = 0; i < 100; ++i)
    {
        samples.range_[i] = 1;
        samples.intensity_[i] = static_cast<std::uint16_t>(i + 1);
    }
    const auto range = blk_core::intensityRange(samples);
    require(range[0] == 1 && range[1] == 99, "Intensity percentiles are wrong");
    const auto image = blk_core::intensityImage(samples, range);
    require(image[0] == 0 && image[98] == 255 && image[99] == 255 && image[100] == 0, "Intensity stretch limits are wrong");
    require(image[49] == std::lround(49 * 255.0 / 98), "Intensity stretch is not linear");
    const std::array<std::uint16_t, 2> none = {0, 0};
    require(blk_core::intensityRange(blk_core::emptyPanorama(4, 2)) == none, "Empty intensity range is wrong");
}

void appendBytes(std::vector<std::uint8_t> &_bytes,
                 const void *_data,
                 std::size_t _size)
{
    const auto *begin = static_cast<const std::uint8_t *>(_data);
    _bytes.insert(_bytes.end(), begin, begin + _size);
}

template<typename T>
std::vector<std::uint8_t> numberBytes(T _value)
{
    std::vector<std::uint8_t> bytes;
    appendBytes(bytes, &_value, sizeof(T));
    return bytes;
}

void appendProperty(std::vector<std::uint8_t> &_bytes,
                    const std::string &_name,
                    std::uint8_t _type,
                    const std::vector<std::uint8_t> &_data)
{
    const auto name_size = static_cast<std::uint16_t>(_name.size() + 1);
    const auto length = static_cast<std::uint16_t>(_data.size());
    appendBytes(_bytes, &name_size, 2);
    appendBytes(_bytes, _name.c_str(), name_size);
    appendBytes(_bytes, &length, 2);
    _bytes.push_back(0);
    _bytes.push_back(0);
    _bytes.push_back(_type);
    _bytes.insert(_bytes.end(), _data.begin(), _data.end());
}

void testCameraMetadata()
{
    std::vector<std::uint8_t> bytes(4, 0);
    const std::uint16_t count = 3;
    appendBytes(bytes, &count, 2);
    appendProperty(bytes, "FocalLength", 11, numberBytes(2400.5));
    appendProperty(bytes, "GainR", 10, numberBytes(1.25f));
    appendProperty(bytes, "GainB", 10, numberBytes(std::numeric_limits<float>::quiet_NaN()));
    const std::uint64_t cursor = 4;
    appendBytes(bytes, &cursor, 8);
    appendBytes(bytes, "HXMETAV1", 8);
    const auto metadata = blk_core::readCameraMetadata(bytes);
    require(metadata.at("FocalLength") == std::vector<double>{2400.5}, "Double camera metadata was not read");
    require(metadata.at("GainR") == std::vector<double>{1.25}, "Float camera metadata was not read");
    require(metadata.count("GainB") == 0, "Non-finite float metadata was kept");
    bytes.back() = '0';
    expectError([&bytes]
    {
        blk_core::readCameraMetadata(bytes);
    });
}

blk_core::RgbCamera uniformCamera(const std::array<double, 9> &_rotation,
                                  std::uint8_t _red,
                                  std::uint8_t _blue)
{
    blk_core::RgbCamera camera;
    camera.rotation = _rotation;
    camera.fx = 32;
    camera.fy = 32;
    camera.cx = 32;
    camera.cy = 32;
    camera.width = 64;
    camera.height = 64;
    camera.pixels.assign(64 * 64 * 3, 0);
    for (std::size_t i = 0; i < camera.pixels.size(); i += 3)
    {
        camera.pixels[i] = _red;
        camera.pixels[i + 2] = _blue;
    }
    return camera;
}

void testProjectAndSample()
{
    blk_core::RgbCamera camera;
    camera.rotation = {1, 0, 0, 0, 1, 0, 0, 0, 1};
    camera.fx = 100;
    camera.fy = 100;
    camera.cx = 50;
    camera.cy = 50;
    camera.width = 100;
    camera.height = 100;
    double px = 0;
    double py = 0;
    double score = 0;
    require(blk_core::projectCamera(camera, {0.2, 0.1, 2}, px, py, score), "Visible point was not projected");
    require(std::abs(px - 60) < 1e-9 && std::abs(py - 55) < 1e-9 && std::abs(score - 0.0125) < 1e-9, "Camera projection is wrong");
    require(!blk_core::projectCamera(camera, {0, 0, -1}, px, py, score), "Point behind the camera was projected");
    blk_core::RgbCamera image;
    image.width = 4;
    image.height = 4;
    image.pixels.assign(4 * 4 * 3, 200);
    for (std::size_t i = 0; i < 2 * 3; ++i)
    {
        image.pixels[i] = static_cast<std::uint8_t>(i / 3 * 100);
    }
    const std::array<std::uint8_t, 3> half = {50, 50, 50};
    const std::array<std::uint8_t, 3> first = {0, 0, 0};
    require(blk_core::sampleBilinear(image, 1.0, 0.5) == half, "Bilinear sample between pixels is wrong");
    require(blk_core::sampleBilinear(image, 0.5, 0.5) == first, "Pixel-centre sample is wrong");
}

void testAccumulateCamera()
{
    auto samples = blk_core::emptyPanorama(16, 8);
    std::fill(samples.range_.begin(), samples.range_.end(), 10.0f);
    const double c = std::cos(blk_core::panoramaPi / 6);
    const double s = std::sin(blk_core::panoramaPi / 6);
    const auto red = uniformCamera({1, 0, 0, 0, 1, 0, 0, 0, 1}, 255, 0);
    const auto blue = uniformCamera({1, 0, 0, 0, c, -s, 0, s, c}, 0, 255);
    const std::size_t zenith = 8;
    const std::size_t nadir = 7 * 16 + 8;
    std::atomic<bool> cancel(false);
    for (const auto &order : {std::array<const blk_core::RgbCamera *, 2>{&blue, &red}, std::array<const blk_core::RgbCamera *, 2>{&red, &blue}})
    {
        std::vector<std::uint8_t> rgb(samples.range_.size() * 3, 0);
        std::vector<float> best(samples.range_.size(), std::numeric_limits<float>::infinity());
        for (const auto *camera : order)
        {
            blk_core::accumulateCamera(*camera, samples, rgb, best, cancel);
        }
        require(rgb[zenith * 3] == 255 && rgb[zenith * 3 + 2] == 0, "Camera closest to the optical axis did not win");
        require(rgb[nadir * 3] == 0 && rgb[nadir * 3 + 2] == 0 && std::isinf(best[nadir]), "Uncovered pixel was coloured");
    }
    std::vector<std::uint8_t> rgb(samples.range_.size() * 3, 0);
    std::vector<float> best(samples.range_.size(), std::numeric_limits<float>::infinity());
    cancel.store(true);
    bool cancelled = false;
    try
    {
        blk_core::accumulateCamera(red, samples, rgb, best, cancel);
    }
    catch (const blk_core::Cancelled &)
    {
        cancelled = true;
    }
    require(cancelled, "Camera accumulation ignored cancellation");
}

void testCameraRecord()
{
    auto camera = uniformCamera({1, 0, 0, 0, 1, 0, 0, 0, 1}, 0, 0);
    camera.translation = {0.1, 0.2, 0.3};
    camera.metadata = {{"ExposureTime", {0.21}}, {"GainR", {1}}, {"GainG", {2}}, {"GainB", {3}}};
    auto record = blk_core::cameraRecord("LDR_Image_9", camera);
    require(record["name"] == "LDR_Image_9" && record["file"] == "LDR_Image_9.png", "Camera record names are wrong");
    require(record["width"] == 64 && record["fx"] == 32.0 && record["rotation"].size() == 9 && record["translation"][2] == 0.3, "Camera calibration was not recorded");
    require(record["exposure_time"] == 0.21 && record["gain_rgb"] == nlohmann::json::array({1.0, 2.0, 3.0}), "Exposure or gains were not recorded");
    require(record["raw"]["GainG"][0] == 2.0, "Raw metadata was not recorded");
    camera.metadata.erase("ExposureTime");
    camera.metadata.erase("GainB");
    record = blk_core::cameraRecord("LDR_Image_9", camera);
    require(record["exposure_time"].is_null() && record["gain_rgb"].is_null(), "Missing metadata was not null");
}

void testOrderedImageNames()
{
    const std::map<std::string, blk_core::Block> blocks = {{"LDR_Image_10", {}}, {"LDR_Image_9", {}}, {"LDR_Image_36", {}}};
    const std::vector<std::string> expected = {"LDR_Image_9", "LDR_Image_10", "LDR_Image_36"};
    require(blk_core::orderedImageNames(blocks) == expected, "Images are not in numeric order");
}

void testParseExportOptions()
{
    const auto defaults = blk_core::parseExportOptions({});
    require(!defaults.panoramas_ && !defaults.camera_images_ && defaults.panorama_width_ == 8192, "Export defaults are wrong");
    const auto both = blk_core::parseExportOptions({"--panoramas", "--camera-images"});
    require(both.panoramas_ && both.camera_images_, "Export flags were not parsed");
    require(blk_core::parseExportOptions({"--panorama-width", "16384"}).panorama_width_ == 16384, "Panorama width was not parsed");
    const std::vector<std::vector<std::string>> invalid = {{"--panorama-width", "5000"}, {"--panorama-width"}, {"--bogus"}};
    for (const auto &flags : invalid)
    {
        expectError([&flags]
        {
            blk_core::parseExportOptions(flags);
        });
    }
}

void testExportPaths()
{
    const auto folder = std::filesystem::path("out");
    const auto ply = folder / "Setup408.ply";
    const auto paths = blk_core::exportPaths(ply);
    require(paths.colour_ == folder / "Setup408_panorama.jpg" && paths.intensity_ == folder / "Setup408_intensity_panorama.png" && paths.images_ == folder / "Setup408_images", "Export paths are wrong");
    require(blk_core::requestedExportPaths(ply, blk_core::ExportOptions()).empty(), "Disabled exports produced paths");
    blk_core::ExportOptions images;
    images.camera_images_ = true;
    require(blk_core::requestedExportPaths(ply, images) == std::vector<std::filesystem::path>{paths.images_}, "Camera image export path is wrong");
    blk_core::ExportOptions all;
    all.panoramas_ = true;
    all.camera_images_ = true;
    const std::vector<std::filesystem::path> ordered = {paths.images_, paths.intensity_, paths.colour_};
    require(blk_core::requestedExportPaths(ply, all) == ordered, "Export path order is wrong");
}

void testRequireExportImages(const std::filesystem::path &_root)
{
    const auto path = _root / "empty.blk";
    blk_core::writeText(path, std::string(4096, '\0'));
    std::string message;
    {
        blk_core::BlkContainer container(path);
        try
        {
            blk_core::requireExportImages(container);
        }
        catch (const std::exception &error)
        {
            message = error.what();
        }
    }
    require(message.find("Turn off the export options") != std::string::npos, "Missing LDR images did not explain how to continue");
}

void testReadIntermediateCloud(const std::filesystem::path &_root)
{
    const auto path = _root / "cloud.ply";
    {
        std::ofstream stream(path, std::ios::binary);
        stream << blk_core::plyHeader(2);
        blk_core::writePoint(stream, blk_core::Point{1, 2, 3, 40});
        blk_core::writePoint(stream, blk_core::Point{4, 5, 6, 50});
    }
    std::atomic<bool> cancel(false);
    const auto cloud = blk_core::readIntermediateCloud(path, 2, cancel);
    const std::array<float, 3> second = {4, 5, 6};
    require(cloud.first.size() == 2 && cloud.first[1] == second && cloud.second[0] == 40 && cloud.second[1] == 50, "Intermediate cloud was not read");
    expectError([&path, &cancel]
    {
        blk_core::readIntermediateCloud(path, 3, cancel);
    });
}

void testConvertRefusesExistingExport(const std::filesystem::path &_root)
{
    const auto output = _root / "convert-out";
    std::filesystem::create_directory(output);
    blk_core::writeText(_root / "synthetic.blk", "invalid");
    const auto existing = output / "synthetic_panorama.jpg";
    blk_core::writeText(existing, "original");
    blk_core::ExportOptions options;
    options.panoramas_ = true;
    std::atomic<bool> cancel(false);
    std::string message;
    try
    {
        blk_core::Converter().convert(_root / "synthetic.blk", output / "synthetic.ply", cancel, [](const blk_core::Progress &)
        {
        }, options);
    }
    catch (const std::exception &error)
    {
        message = error.what();
    }
    require(message.find("synthetic_panorama.jpg") != std::string::npos, "Existing panorama was not reported");
    require(blk_core::readText(existing) == "original" && !std::filesystem::exists(output / "synthetic.ply"), "Existing export output was modified");
}

void testPublishConversion(const std::filesystem::path &_root)
{
    blk_core::ExportOptions options;
    options.panoramas_ = true;
    const auto prepare = [&_root](const std::string &_name)
    {
        const auto work = _root / _name;
        std::filesystem::create_directory(work);
        blk_core::writeText(work / "intensity_panorama.png", "png");
        blk_core::writeText(work / "panorama.jpg", "jpg");
        return work;
    };
    const auto output = _root / "publish-out";
    std::filesystem::create_directory(output);
    const auto blocked = prepare("work-blocked");
    blk_core::writeText(blocked / "cloud.ply", "ply");
    blk_core::writeText(output / "a.ply", "original");
    expectError([&blocked, &output, &options]
    {
        blk_core::publishConversion(blocked, output / "a.ply", options);
    });
    require(!std::filesystem::exists(output / "a_panorama.jpg") && !std::filesystem::exists(output / "a_intensity_panorama.png"), "Exports were published before a refused PLY");
    require(blk_core::readText(output / "a.ply") == "original", "Existing PLY was modified");
    const auto missing = prepare("work-missing");
    std::string message;
    try
    {
        blk_core::publishConversion(missing, output / "b.ply", options);
    }
    catch (const std::exception &error)
    {
        message = error.what();
    }
    require(message.find("Already published") != std::string::npos && message.find("b_panorama.jpg") != std::string::npos, "Published exports were not reported after the PLY failed");
}

void testQueueExportCollision(const std::filesystem::path &_root)
{
    const auto project_root = _root / "project";
    const auto output = project_root / "output";
    const auto id = blk_core::newId();
    {
        blk_core::Project project(project_root);
        project.setOutput(output.u8string());
    }
    auto manifest = nlohmann::json::parse(blk_core::readText(project_root / "project.json"));
    manifest["jobs"].push_back({{"id", id}, {"name", "synthetic.blk"}, {"source_path", (project_root / "synthetic.blk").u8string()}, {"status", "running"}});
    blk_core::writeText(project_root / "project.json", manifest.dump());
    blk_core::writeText(project_root / "synthetic.blk", "invalid");
    std::filesystem::create_directories(output);
    blk_core::writeText(output / "synthetic_panorama.jpg", "original");
    blk_core::Project project(project_root);
    project.retry(id);
    blk_core::ExportOptions options;
    options.panoramas_ = true;
    expectError([&project, &options]
    {
        project.queue(nullptr, options);
    });
    auto job = project.snapshot()["jobs"][0];
    require(job["status"] == "ready" && !job.contains("export"), "Refused export queue changed the job");
    project.queue(nullptr, blk_core::ExportOptions());
    job = project.snapshot()["jobs"][0];
    require(job["export"] == nlohmann::json({{"panoramas", false}, {"camera_images", false}}), "Export options were not recorded on the job");
    require(blk_core::readText(output / "synthetic_panorama.jpg") == "original", "Existing panorama was modified");
}

void testExportFilePath()
{
    const auto job = nlohmann::json{{"status", "completed"}, {"result", {{"panorama", {{"colour_path", "/o/a_panorama.jpg"}, {"intensity_path", "/o/a_intensity_panorama.png"}, {"images_path", "/o/a_images"}}}}}};
    require(blk_core::exportFilePath(job, "colour") == std::filesystem::u8path("/o/a_panorama.jpg"), "Colour panorama path is wrong");
    require(blk_core::exportFilePath(job, "intensity") == std::filesystem::u8path("/o/a_intensity_panorama.png"), "Intensity panorama path is wrong");
    require(blk_core::exportFilePath(job, "cameras.json") == std::filesystem::u8path("/o/a_images") / "cameras.json", "Camera metadata path is wrong");
    require(blk_core::exportFilePath(job, "LDR_Image_9.png") == std::filesystem::u8path("/o/a_images") / "LDR_Image_9.png", "Camera image path is wrong");
    for (const std::string item : {"", "../x", "LDR_Image_9.jpg", "LDR_Image_.png", "LDR_Image_9.png/../../x", "bogus"})
    {
        expectError([&job, &item]
        {
            blk_core::exportFilePath(job, item);
        });
    }
    auto panoramas_only = job;
    panoramas_only["result"]["panorama"].erase("images_path");
    expectError([&panoramas_only]
    {
        blk_core::exportFilePath(panoramas_only, "cameras.json");
    });
    auto ready = job;
    ready["status"] = "ready";
    expectError([&ready]
    {
        blk_core::exportFilePath(ready, "colour");
    });
    const auto unexported = nlohmann::json{{"status", "completed"}};
    expectError([&unexported]
    {
        blk_core::exportFilePath(unexported, "colour");
    });
}

int main()
{
    const auto root = std::filesystem::temp_directory_path() / ("blk-panorama-test-" + blk_core::newId());
    try
    {
        std::filesystem::create_directory(root);
        testImageWriters(root);
        testPublishDirectory(root);
        testPanoramaPixel();
        testPanoramaRoundTrip();
        testSplatNearest();
        testFillHoles();
        testIntensityImage();
        testCameraMetadata();
        testProjectAndSample();
        testAccumulateCamera();
        testCameraRecord();
        testOrderedImageNames();
        testParseExportOptions();
        testExportPaths();
        testRequireExportImages(root);
        testReadIntermediateCloud(root);
        testConvertRefusesExistingExport(root);
        testPublishConversion(root);
        testQueueExportCollision(root);
        testExportFilePath();
        std::filesystem::remove_all(root);
        std::cout << "Panorama checks passed\n";
        return 0;
    }
    catch (const std::exception &error)
    {
        std::error_code ignored;
        std::filesystem::remove_all(root, ignored);
        std::cerr << error.what() << '\n';
        return 1;
    }
}
