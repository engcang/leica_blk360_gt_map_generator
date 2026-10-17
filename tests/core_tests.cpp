#include <iostream>

#include "project.hpp"
#include "file_browser.hpp"

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
            std::filesystem::last_write_time(saved, std::filesystem::file_time_type::clock::now() - std::chrono::hours(1));
            require(blk_core::inspectPly(saved)["valid_points"] == 2, "Saved PLY count failed");
            std::string cloud_id;
            {
                blk_core::Project project(root);
                require(project.snapshot()["point_clouds"].empty(), "Output PLY discovered without explicit selection");
                const auto before_view = blk_core::readText(root / "project.json");
                const auto folder = project.addPointClouds({saved.parent_path().u8string()});
                require(folder["added"] == 1 && folder["errors"].size() == 1, "Explicit folder import did not report valid and invalid PLYs");
                require(project.snapshot()["jobs"].empty(), "PLY discovery created conversion jobs");
                require(project.addPointClouds({external.u8string()})["added"] == 1, "External PLY not added");
                require(project.addPointClouds({external.u8string()})["skipped"] == 1, "Duplicate PLY not recognized");
                require(blk_core::readText(root / "project.json") == before_view, "Viewing persisted a point-cloud cache");
                cloud_id = project.snapshot()["point_clouds"][1]["id"];
                require(project.pointCloud(cloud_id)["valid_points"] == 2, "External PLY not viewable");
                expectError([&project]
                {
                    project.pointCloud("../../outside");
                });
            }
            {
                blk_core::Project project(root);
                require(project.snapshot()["point_clouds"].empty(), "PLY selection restored on restart");
                expectError([&project, &cloud_id]
                {
                    project.pointCloud(cloud_id);
                });
                require(project.addPointClouds({saved.u8string(), external.u8string()})["added"] == 2, "Explicit reselection failed");
                cloud_id = project.snapshot()["point_clouds"][1]["id"];
                std::filesystem::remove(external);
                require(project.snapshot()["point_clouds"].size() == 1, "Deleted PLY remained in memory");
                project.refreshClouds();
                require(project.snapshot()["point_clouds"].size() == 1 && project.snapshot()["point_cloud_errors"].empty(), "Refresh rediscovered unselected files");
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
            const auto history_root = root / "history";
            std::filesystem::create_directory(history_root);
            const auto present_id = blk_core::newId(), missing_id = blk_core::newId(), unavailable_id = blk_core::newId();
            auto history = nlohmann::json::parse(blk_core::readText(root / "project.json"));
            history["jobs"] = nlohmann::json::array();
            for (const auto &entry : {std::make_pair(present_id, saved), std::make_pair(missing_id, root / "deleted.ply"), std::make_pair(unavailable_id, root / "absent.ply")})
                history["jobs"].push_back({{"id", entry.first}, {"name", "synthetic.blk"}, {"source_path", (entry.first == unavailable_id ? root / "missing.blk" : root / "synthetic.blk").u8string()}, {"status", "completed"}, {"output_path", entry.second.u8string()}, {"progress", 1.0}, {"result", {{"valid_points", 2}}}});
            blk_core::writeText(history_root / "project.json", history.dump());
            {
                blk_core::Project project(history_root);
                require(project.snapshot()["jobs"].size() == 2, "Completed history was restored");
                const auto missing = project.scan(missing_id);
                require(missing["status"] == "ready" && missing["result"].is_null() && missing["progress"] == 0 && missing["output_path"] == "", "Deleted PLY was still completed");
                require(project.scan(unavailable_id)["status"] == "failed", "Missing source was made ready");
                expectError([&project, &present_id]
                {
                    project.scan(present_id);
                });
                expectError([&project, &missing_id]
                {
                    project.outputPath(missing_id);
                });
                for (const auto &name : {"project.json", "project.json.backup"})
                {
                    const auto stored = nlohmann::json::parse(blk_core::readText(history_root / name));
                    for (const auto &job : stored["jobs"])
                        require(job["status"] != "completed", "Completed history was retained on disk");
                }
            }
            const auto ordered_root = root / "ordered";
            std::filesystem::create_directory(ordered_root);
            const auto oldest = ordered_root / "Setup511.ply";
            const auto middle = ordered_root / "Setup52.ply";
            const auto newest = ordered_root / "Setup51.ply";
            const auto stamp = std::filesystem::file_time_type::clock::now() - std::chrono::hours(2);
            for (const auto &path : {oldest, middle, newest})
                blk_core::writeText(path, ply);
            std::filesystem::last_write_time(oldest, stamp);
            std::filesystem::last_write_time(middle, stamp + std::chrono::seconds(100));
            std::filesystem::last_write_time(newest, stamp + std::chrono::seconds(200));
            {
                blk_core::Project project(root / "ordered-project");
                project.addPointClouds({newest.u8string()});
                auto selected = project.addPointClouds({middle.u8string(), oldest.u8string()});
                require(selected["clouds"][0]["name"] == "Setup511.ply", "File selection followed click/name order");
                const auto clouds = project.snapshot()["point_clouds"];
                require(clouds[0]["name"] == "Setup511.ply" && clouds[1]["name"] == "Setup52.ply" && clouds[2]["name"] == "Setup51.ply", "Separate additions were not sorted by modification time");
                selected = project.addPointClouds({ordered_root.u8string()});
                require(selected["clouds"] == clouds, "Folder and explicit file orders differ");
                auto entries = blk_core::browseFiles(ordered_root.u8string(), ".ply")["entries"];
                require(entries[0]["name"] == "Setup511.ply" && entries[2]["name"] == "Setup51.ply", "Browser is not chronological");
                std::filesystem::last_write_time(newest, stamp);
                entries = blk_core::browseFiles(ordered_root.u8string(), ".ply")["entries"];
                require(entries[0]["name"] == "Setup51.ply" && entries[1]["name"] == "Setup511.ply", "Equal-time tie is not deterministic");
                auto records = nlohmann::json::array({{{"source_path", middle.u8string()}}, {{"source_path", oldest.u8string()}}, {{"source_path", (ordered_root / "missing.blk").u8string()}}});
                blk_core::sortRecordsByModification(records, "source_path");
                require(records[0]["source_path"] == oldest.u8string() && records[2]["source_path"] == (ordered_root / "missing.blk").u8string(), "Conversion record order or missing-file fallback failed");
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
