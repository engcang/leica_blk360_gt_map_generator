#include <iostream>

#include "project.hpp"

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

int main()
{
    try
    {
        blk_core::requireLittleEndian();
        blk_core::ScanModel model;
        model.rows_ = 100;
        model.range_scale_ = 0.001;
        model.cone_scale_ = 0.001;
        model.phi_scale_ = 0.001;
        std::array<double, 14> line = {};
        line[0] = 1;
        const auto point = blk_core::reconstruct(model, line, 0, 2000, 0, 0, 100);
        if (point.x_ != 0 || point.y_ != 2 || point.z_ != 0 || point.intensity_ != 100)
        {
            throw std::runtime_error("Analytic scan frame reconstruction failed");
        }
        if (blk_core::base64({1, 2, 3}) != "AQID")
        {
            throw std::runtime_error("Base64 failed");
        }
        line[0] = 0;
        line[1] = 1;
        const auto rotated = blk_core::reconstruct(model, line, 0, 2000, 0, 0, 65535);
        require(rotated.x_ == -2 && rotated.y_ == 0 && rotated.z_ == 0 && rotated.intensity_ == 65535, "Azimuth frame failed");
        line[0] = 1;
        line[1] = 0;
        line[3] = std::acos(-1.0) / 2;
        model.cone_offset_ = 100;
        const auto vertical = blk_core::reconstruct(model, line, 0, 2000, 50, 0, 10);
        require(std::abs(vertical.x_ - 0.15) < 1e-6 && std::abs(vertical.y_) < 1e-6 && vertical.z_ == 2, "Residual quantization or elevation failed");
        expectError([]
        {
            blk_core::readNumber<double>({1, 2}, 0);
        });
        std::atomic<bool> cancelled(true);
        expectError([&cancelled]
        {
            blk_core::checkCancel(cancelled);
        });
        const auto root = std::filesystem::temp_directory_path() / ("blk-test-" + blk_core::newId());
        std::filesystem::create_directory(root);
        try
        {
            const auto id = blk_core::newId();
            {
                blk_core::InstanceLock first(root / "lock");
                blk_core::InstanceLock second(root / "lock");
                require(first.acquired() && !second.acquired(), "Project lock failed");
                blk_core::Project project(root);
                project.rename("persistent project");
                project.setOutput((root / "output").u8string());
                blk_core::writeText(root / "synthetic.blk", "invalid");
                const auto added = project.addPaths({(root / "synthetic.blk").u8string()});
                require(added["added"] == 0 && added["errors"].size() == 1, "Invalid BLK accepted");
                require(!std::filesystem::exists(root / "inputs"), "Source storage was created");
                expectError([&project]
                {
                    project.outputPath("../../outside");
                });
                expectError([&project]
                {
                    project.setOutput("");
                });
            }
            auto manifest = nlohmann::json::parse(blk_core::readText(root / "project.json"));
            manifest["jobs"].push_back({{"id", id}, {"name", "synthetic.blk"}, {"source_path", (root / "synthetic.blk").u8string()}, {"status", "running"}});
            blk_core::writeText(root / "project.json", manifest.dump());
            {
                blk_core::Project project(root);
                require(project.snapshot()["name"] == "persistent project", "Project persistence failed");
                require(project.snapshot()["jobs"][0]["status"] == "interrupted", "Interrupted job recovery failed");
                project.retry(id);
                require(project.snapshot()["jobs"][0]["status"] == "ready", "Retry failed");
                const auto target = root / "output" / "synthetic.ply";
                std::filesystem::create_directory(root / "output");
                blk_core::writeText(target, "original");
                expectError([&project]
                {
                    project.queue();
                });
                expectError([&root, &target, &cancelled]
                {
                    blk_core::Converter().convert(root / "synthetic.blk", target, cancelled, [](const blk_core::Progress &)
                    {
                    });
                });
                const auto pending = root / "pending.ply";
                blk_core::writeText(pending, "new output");
                expectError([&pending, &target]
                {
                    blk_core::publishFile(pending, target);
                });
                require(blk_core::readText(target) == "original", "Existing output was modified");
                blk_core::publishFile(pending, root / "published.ply");
                require(blk_core::readText(root / "published.ply") == "new output", "Publish failed");
                project.remove(id);
                require(std::filesystem::exists(target) && std::filesystem::exists(root / "synthetic.blk"), "Removing a job deleted user files");
            }
            const auto saved = root / "output" / "saved.ply";
            const auto external = root / "external.PLY";
            const std::string ply = "ply\nformat binary_little_endian 1.0\ncomment saved cloud\nelement vertex 2\nproperty float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n" + std::string(28, '\0');
            blk_core::writeText(saved, ply);
            blk_core::writeText(external, ply);
            require(blk_core::inspectPly(saved)["valid_points"] == 2, "Saved PLY count failed");
            std::string cloud_id;
            {
                blk_core::Project project(root);
                require(project.snapshot()["point_clouds"].size() == 1, "Output PLY not discovered at startup");
                require(project.snapshot()["jobs"].empty(), "PLY discovery created conversion jobs");
                require(project.addPointClouds({external.u8string()})["added"] == 1, "External PLY not added");
                require(project.addPointClouds({external.u8string()})["skipped"] == 1, "Duplicate PLY not recognized");
                cloud_id = project.snapshot()["point_clouds"][1]["id"];
                require(project.pointCloud(cloud_id)["valid_points"] == 2, "External PLY not viewable");
                expectError([&project]
                {
                    project.pointCloud("../../outside");
                });
            }
            {
                blk_core::Project project(root);
                require(project.snapshot()["point_clouds"].size() == 2, "Saved PLY library lost on restart");
                require(project.pointCloud(cloud_id)["path"] == std::filesystem::canonical(external).u8string(), "Saved PLY ID changed on restart");
                std::filesystem::remove(external);
                require(project.snapshot()["point_clouds"].size() == 1, "Deleted PLY remained in the saved-cloud list");
                project.refreshClouds();
                const auto errors = project.snapshot()["point_cloud_errors"];
                require(errors.size() == 1 && std::filesystem::equivalent(std::filesystem::u8path(errors[0]["path"].get<std::string>()), root / "output" / "synthetic.ply"), "Missing-cloud cleanup lost an invalid-file error or retained a stale warning");
                std::filesystem::remove(root / "output" / "synthetic.ply");
                require(project.snapshot()["point_cloud_errors"].empty(), "Deleted invalid PLY retained a warning");
                expectError([&project, &cloud_id]
                {
                    project.pointCloud(cloud_id);
                });
                blk_core::writeText(external, ply.substr(0, ply.size() - 1));
                expectError([&external]
                {
                    blk_core::inspectPly(external);
                });
                require(project.addPointClouds({external.u8string()})["errors"].size() == 1, "Truncated PLY accepted");
                blk_core::writeText(external, ply);
                require(project.addPointClouds({external.u8string()})["added"] == 1, "Restored external PLY could not be added again");
                require(project.snapshot()["point_clouds"][1]["available"] == true, "Restored PLY did not become available");
                require(blk_core::readText(saved) == ply && blk_core::readText(external) == ply, "Viewing changed PLY data");
                require(!std::filesystem::exists(root / "inputs") && !std::filesystem::exists(root / "scans"), "PLY data was copied into the project");
            }
            std::filesystem::remove_all(root);
        }
        catch (...)
        {
            std::filesystem::remove_all(root);
            throw;
        }
        std::cout << "Core checks passed\n";
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
