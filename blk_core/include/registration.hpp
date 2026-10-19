#pragma once

#include <algorithm>
#include <atomic>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <json.hpp>

#include "project.hpp"
#include "placement.hpp"
#include "worker_process.hpp"

namespace blk_core
{
    class Registration
    {
    public:
        Registration(Project &_project,
                     const std::filesystem::path &_scripts):
            project_(_project), scripts_(_scripts), root_(std::filesystem::u8path(_project.snapshot().at("project_directory").get<std::string>()))
        {
            const auto file = root_ / "registration_jobs.json";
            if (std::filesystem::exists(file))
            {
                state_ = readJson(file);
            }
            else
            {
                state_ = {{"schema_version", 1}, {"python", defaultPython()}, {"runtime", {{"status", "unchecked"}}}, {"jobs", nlohmann::json::array()}};
            }
            if (state_.contains("draft") && state_["draft"].is_object())
            {
                state_["draft"].erase("cloud_ids");
                state_["draft"].erase("initial_guesses");
            }
            for (auto &job : state_.at("jobs"))
            {
                if (job.contains("products"))
                {
                    for (auto &[kind, product] : job["products"].items())
                        product.erase("cloud_id");
                }
                if (job.contains("export") && job["export"].value("status", "") == "running")
                {
                    job["export"]["status"] = "interrupted";
                }
                if (job.value("status", "") == "running")
                {
                    job["status"] = "interrupted";
                    job["stage"] = "App stopped before registration finished. Resume to reuse completed scan caches.";
                }
            }
            if (state_["runtime"].value("status", "") == "running")
            {
                state_["runtime"]["status"] = "interrupted";
            }
            if (!state_["runtime"].contains("native_backend"))
            {
                state_["runtime"]["status"] = "unchecked";
                state_["runtime"]["stage"] = "Check the KISS-Matcher / Nano-GICP runtime after updating BLK Studio.";
            }
            saveLocked();
        }

        ~Registration()
        {
            cancel_.store(true);
            if (worker_.joinable())
            {
                worker_.join();
            }
        }

        void cancel()
        {
            cancel_.store(true);
        }

        void saveDraft(const nlohmann::json &_draft)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!_draft.is_object())
            {
                throw std::runtime_error("Registration selection must be an object");
            }
            state_["draft"] = _draft;
            state_["draft"].erase("cloud_ids");
            state_["draft"].erase("initial_guesses");
            saveLocked();
        }

        nlohmann::json placements(const nlohmann::json &_body, bool _save)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (_body.contains("resume_id"))
            {
                const auto &job = findJob(state_, _body.at("resume_id").get<std::string>());
                const auto directory = std::filesystem::u8path(job.at("output_path").get<std::string>()).parent_path();
                const auto chunks = readJson(directory / "partial_chunks.json").at("chunks");
                const auto file = directory / "chunk_initial_placements.json";
                auto document = nlohmann::json{{"schema_version", 1}, {"chunks", chunks}, {"poses", nlohmann::json::object()}};
                if (std::filesystem::exists(file))
                {
                    document = readJson(file);
                    if (document.at("chunks") != chunks)
                        throw std::runtime_error("Saved chunk placements refer to different chunks. Preserve the saved JSON before starting a new arrangement.");
                }
                auto values = nlohmann::json::array();
                if (_save && _body.at("initial_guesses").size() != chunks.size())
                    throw std::runtime_error("Chunk placement count changed");
                for (std::size_t i = 0; i < chunks.size(); ++i)
                {
                    const auto key = chunks[i].at("key").get<std::string>();
                    auto pose = _save ? _body.at("initial_guesses")[i] : document["poses"].value(key, nlohmann::json());
                    validatePlacement(pose);
                    document["poses"][key] = pose;
                    values.push_back(pose);
                }
                if (_save)
                {
                    saveJson(file, document);
                    if (readJson(file) != document)
                        throw std::runtime_error("Chunk placement save verification failed");
                }
                return {{"path", file.u8string()}, {"initial_guesses", values}};
            }
            const auto directory_text = _body.at("output_directory").get<std::string>();
            if (directory_text.empty())
                throw std::runtime_error("Choose an output folder for placements");
            const auto directory = std::filesystem::weakly_canonical(std::filesystem::absolute(std::filesystem::u8path(directory_text)));
            const auto file = directory / "initial_placements.json";
            auto document = placementDocument(file);
            const auto &ids = _body.at("cloud_ids");
            if (!ids.is_array() || ids.size() < 2)
                throw std::runtime_error("Select at least two scans");
            if (_save && (!_body.at("initial_guesses").is_array() || _body.at("initial_guesses").size() != ids.size()))
                throw std::runtime_error("Placement count does not match the selected scans");
            auto values = nlohmann::json::array();
            for (std::size_t i = 0; i < ids.size(); ++i)
            {
                const auto cloud = project_.pointCloud(ids[i].get<std::string>());
                const auto key = std::filesystem::weakly_canonical(std::filesystem::u8path(cloud.at("path").get<std::string>())).u8string();
                if (_save)
                {
                    const auto &pose = _body.at("initial_guesses")[i];
                    validatePlacement(pose);
                    document["poses"][key] = pose;
                }
                auto pose = document["poses"].contains(key) ? document["poses"][key] : nlohmann::json();
                validatePlacement(pose);
                values.push_back(pose);
            }
            if (_save)
            {
                std::filesystem::create_directories(directory);
                saveJson(file, document);
                if (readJson(file) != document)
                    throw std::runtime_error("Placement save verification failed");
            }
            return {{"path", file.u8string()}, {"initial_guesses", values}};
        }

        nlohmann::json snapshot()
        {
            std::lock_guard<std::mutex> lock(mutex_);
            auto result = state_;
            result["busy"] = busy_;
            for (auto &entry : result["jobs"])
            {
                if (!entry.contains("poses_path"))
                {
                    continue;
                }
                const auto directory = std::filesystem::u8path(entry.at("poses_path").get<std::string>()).parent_path();
                entry["chunks_available"] = std::filesystem::is_regular_file(directory / "chunks_review.html");
                const auto products_file = directory / "html_source" / "products.json";
                if (std::filesystem::is_regular_file(products_file))
                {
                    try
                    {
                        auto products = readJson(products_file);
                        for (auto &[kind, product] : products.items())
                        {
                            const auto filename = std::filesystem::u8path(product.at("file").get<std::string>());
                            product["available"] = filename == filename.filename() && std::filesystem::is_regular_file(directory / filename);
                            if (entry.contains("products") && entry["products"].contains(kind) && entry["products"][kind].contains("cloud_id"))
                            {
                                product["cloud_id"] = entry["products"][kind]["cloud_id"];
                            }
                        }
                        entry["products"] = products;
                    }
                    catch (const std::exception &)
                    {
                        entry["products"] = nlohmann::json::object();
                    }
                }
            }
            auto &job = active_id_.empty() ? result["runtime"] : findJob(result, active_id_);
            auto &active = active_export_ ? job["export"] : job;
            if (active.contains("report_directory"))
            {
                const auto report = std::filesystem::u8path(active.at("report_directory").get<std::string>());
                if (busy_ && std::filesystem::exists(report / "progress.json"))
                {
                    try
                    {
                        active["stage"] = readJson(report / "progress.json").value("stage", "Working");
                    }
                    catch (const std::exception &)
                    {
                    }
                }
                result["log"] = readLog(report / "worker.log");
            }
            return result;
        }

        void setup(const nlohmann::json &_body)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            requireIdleLocked();
            const auto python = _body.value("python", state_.value("python", defaultPython()));
            if (python.empty() || python.front() == '-' || python.find('\0') != std::string::npos)
            {
                throw std::runtime_error("Enter a Python executable path or command, without arguments");
            }
            const auto report = root_ / "registration_runtime" / newId();
            std::filesystem::create_directories(report);
            state_["python"] = python;
            state_["runtime"] = {{"status", "running"}, {"stage", "Checking registration runtime"}, {"report_directory", report.u8string()}};
            std::vector<std::string> arguments{python, "-u", (scripts_ / "studio_worker.py").u8string()};
            if (_body.value("install", false))
            {
                arguments.push_back("--setup");
                arguments.push_back((root_ / "mapping-runtime").u8string());
            }
            else
            {
                arguments.push_back("--check");
            }
            arguments.insert(arguments.end(), {"--report", report.u8string()});
            launchLocked("", report, arguments);
        }

        void generateProduct(const std::string &_id,
                             const std::string &_kind)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            requireIdleLocked();
            auto &job = findJob(state_, _id);
            if (job.value("status", "") != "completed")
            {
                throw std::runtime_error("Complete registration before generating products");
            }
            if (_kind != "raw" && _kind != "voxel" && _kind != "share")
            {
                throw std::runtime_error("Choose raw, voxel, or share");
            }
            if (state_["runtime"].value("status", "") != "completed")
            {
                throw std::runtime_error("Check the registration runtime first");
            }
            const auto report = root_ / "registration_exports" / newId();
            const auto source = std::filesystem::u8path(job.at("poses_path").get<std::string>());
            std::filesystem::create_directories(report);
            job["export"] = {{"status", "running"}, {"product", _kind}, {"report_directory", report.u8string()}};
            launchLocked(_id, report, {state_.at("python").get<std::string>(), "-u", (scripts_ / "studio_worker.py").u8string(), "--generate", source.u8string(), "--product", _kind, "--report", report.u8string()}, true);
        }

        std::string importResult(const std::string &_path)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            requireIdleLocked();
            auto path = std::filesystem::canonical(std::filesystem::u8path(_path));
            const auto directory = std::filesystem::is_directory(path) ? path : path.parent_path();
            if (std::filesystem::is_directory(path))
                path = directory / "relative_poses.json";
            for (const auto &job : state_["jobs"])
            {
                const auto output = std::filesystem::u8path(job.value("poses_path", "")).parent_path();
                if (!output.empty() && std::filesystem::weakly_canonical(output) == directory &&
                    (path.filename() == "relative_poses.json" || path.filename() == "partial_chunks.json"))
                    return job.at("id").get<std::string>();
            }
            if (path.filename() != "relative_poses.json" || !std::filesystem::is_regular_file(directory / "result_review_and_manage.html"))
            {
                throw std::runtime_error("Select relative_poses.json beside result_review_and_manage.html");
            }
            const auto manifest = readJson(path);
            if (manifest.value("schema_version", 0) != 3 || !manifest.at("scans").is_array() || manifest.at("scans").empty())
            {
                throw std::runtime_error("Expected a BLK Studio poses-only registration result");
            }
            std::string id;
            for (const auto &job : state_["jobs"])
            {
                if (std::filesystem::u8path(job.value("poses_path", "")).lexically_normal() == path.lexically_normal())
                {
                    id = job.at("id");
                    break;
                }
            }
            if (id.empty())
            {
                id = newId();
                state_["jobs"].push_back({{"id", id}, {"name", manifest.value("result_name", "reference_map")}, {"status", "completed"}, {"stage", "Loaded saved poses and review; registration was not repeated"}, {"scans", manifest.at("scans")}, {"voxel_m", manifest.at("registration_voxel_m")}, {"poses_path", path.u8string()}, {"output_path", (directory / "reference_map_1cm.ply").u8string()}, {"review_path", (directory / "result_review_and_manage.html").u8string()}, {"review_available", true}});
            }
            const auto products_file = directory / "html_source" / "products.json";
            if (std::filesystem::is_regular_file(products_file))
            {
                const auto products = readJson(products_file);
                for (const auto &[kind, product] : products.items())
                {
                    const auto filename = std::filesystem::u8path(product.value("file", ""));
                    const auto ply = directory / filename;
                    if ((kind != "raw" && kind != "voxel") || filename != filename.filename() || !std::filesystem::is_regular_file(ply))
                    {
                        continue;
                    }
                    const auto imported = project_.addPointClouds({ply.u8string()});
                    if (!imported.at("errors").empty())
                    {
                        continue;
                    }
                    const auto clouds = project_.snapshot();
                    for (const auto &cloud : clouds.at("point_clouds"))
                    {
                        if (std::filesystem::equivalent(std::filesystem::u8path(cloud.at("path").get<std::string>()), ply))
                        {
                            findJob(state_, id)["products"][kind] = product;
                            findJob(state_, id)["products"][kind]["cloud_id"] = cloud.at("id");
                        }
                    }
                }
            }
            saveLocked();
            return id;
        }

        std::filesystem::path resultFile(const std::string &_id,
                                         const std::string &_relative)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            const auto &job = findJob(state_, _id);
            const auto root = std::filesystem::u8path(job.at("poses_path").get<std::string>()).parent_path();
            const auto relative = std::filesystem::u8path(_relative);
            if (relative.is_absolute() || _relative.find("..") != std::string::npos || _relative.find('\\') != std::string::npos)
            {
                throw std::runtime_error("Invalid result asset path");
            }
            bool allowed = _relative == "relative_poses.json" || _relative == "result_review_and_manage.html" ||
                           (relative.parent_path() == "html_source" && relative.filename() != "products.json");
            const auto products_path = root / "html_source" / "products.json";
            if (!allowed && relative == relative.filename() && std::filesystem::is_regular_file(products_path))
            {
                for (const auto &product : readJson(products_path))
                {
                    allowed = allowed || product.value("file", "") == _relative;
                }
            }
            const auto target = std::filesystem::weakly_canonical(root / relative);
            const auto within = target.lexically_relative(std::filesystem::canonical(root));
            if (!allowed || within.empty() || *within.begin() == "..")
            {
                throw std::runtime_error("Result asset is not available");
            }
            return target;
        }

        void start(const nlohmann::json &_body)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            requireIdleLocked();
            if (state_["runtime"].value("status", "") != "completed")
            {
                throw std::runtime_error("Check or install the registration runtime first");
            }
            const auto resume_id = _body.value("resume_id", "");
            std::filesystem::path report;
            std::string id;
            nlohmann::json config;
            if (!resume_id.empty())
            {
                auto &job = findJob(state_, resume_id);
                if (job.value("status", "") == "completed")
                {
                    throw std::runtime_error("This map is already complete");
                }
                id = resume_id;
                report = std::filesystem::u8path(job.at("report_directory").get<std::string>());
                config = readJson(report / "config.json");
                validatePairs(_body.value("additional_pairs", ""), config.at("paths").size());
                config["additional_pairs"] = config.value("additional_pairs", "") + "," + _body.value("additional_pairs", "");
            }
            else
            {
                const auto name = _body.at("name").get<std::string>();
                if (name.empty() || name.size() > 100 || name == "." || name == ".." || name.back() == '.' || name.back() == ' ' ||
                    std::any_of(name.begin(), name.end(), [](unsigned char _character)
                {
                    return _character < 32 || std::string("<>:\"/\\|?*").find(static_cast<char>(_character)) != std::string::npos;
                }))
                {
                    throw std::runtime_error("Enter a map name without a file extension or path separators");
                }
                const double voxel = _body.at("voxel_m").get<double>();
                if (!std::isfinite(voxel) || voxel != 0.01)
                {
                    throw std::runtime_error("This registration pipeline refines and evaluates on 1 cm clouds");
                }
                const auto directory_text = _body.at("output_directory").get<std::string>();
                if (directory_text.empty())
                {
                    throw std::runtime_error("Choose an output folder for the map");
                }
                const auto directory = std::filesystem::weakly_canonical(std::filesystem::absolute(std::filesystem::u8path(directory_text)));
                const auto target = directory / std::filesystem::u8path(name + "_1cm.ply");
                const auto poses = directory / "relative_poses.json";
                const auto review = directory;
                if (std::filesystem::exists(target))
                {
                    throw std::runtime_error("Map PLY already exists. Choose a new map name");
                }
                if (std::filesystem::exists(poses))
                {
                    throw std::runtime_error("relative_poses.json already exists. Open that result or choose a different output folder");
                }
                if (std::filesystem::exists(review / "html_source") || std::filesystem::exists(review / "result_review_and_manage.html"))
                {
                    throw std::runtime_error("Review output already exists. Choose a different output folder");
                }
                nlohmann::json paths = nlohmann::json::array();
                nlohmann::json scans = nlohmann::json::array();
                for (const auto &cloud_id : _body.at("cloud_ids"))
                {
                    const auto cloud = project_.pointCloud(cloud_id.get<std::string>());
                    for (const auto &path : paths)
                    {
                        if (std::filesystem::equivalent(std::filesystem::u8path(path.get<std::string>()), std::filesystem::u8path(cloud.at("path").get<std::string>())))
                        {
                            throw std::runtime_error("The same scan was selected more than once");
                        }
                    }
                    paths.push_back(cloud.at("path"));
                    scans.push_back({{"id", cloud_id}, {"name", cloud.at("name")}, {"path", cloud.at("path")}});
                }
                if (paths.size() < 2)
                {
                    throw std::runtime_error("Select at least two PLY scans in acquisition order");
                }
                validatePairs(_body.value("additional_pairs", ""), paths.size());
                if (_body.contains("chunk_initial_guesses"))
                    throw std::runtime_error("Chunk placements require an existing registration");
                if (_body.contains("initial_guesses"))
                {
                    const auto &guesses = _body.at("initial_guesses");
                    if (!guesses.is_array() || guesses.size() != paths.size())
                        throw std::runtime_error("Placement selection changed; reopen the placement editor");
                    for (const auto &pose : guesses)
                        validatePlacement(pose);
                }
                verifySavedPlacements(_body, paths, directory);
                id = newId();
                report = root_ / "registration_records" / id;
                std::filesystem::create_directories(directory);
                if (!std::filesystem::create_directory(directory / "html_source"))
                {
                    throw std::runtime_error("This result folder is already in use. Choose a different output folder");
                }
                if (!std::filesystem::create_directories(report))
                {
                    throw std::runtime_error("Registration record folder already exists. Resume the saved job or choose a new map name");
                }
                config = {{"name", name}, {"paths", paths}, {"output_path", target.u8string()}, {"poses_path", poses.u8string()}, {"review_directory", review.u8string()}, {"voxel_m", voxel}, {"additional_pairs", _body.value("additional_pairs", "")}};
                state_["jobs"].push_back({{"id", id}, {"name", name}, {"scans", scans}, {"voxel_m", voxel}, {"output_path", target.u8string()}, {"poses_path", poses.u8string()}, {"review_path", (review / "result_review_and_manage.html").u8string()}, {"report_directory", report.u8string()}});
            }
            if (std::filesystem::exists(std::filesystem::u8path(config.at("output_path").get<std::string>())))
            {
                throw std::runtime_error("Map PLY already exists; open it from View saved point clouds or use a new map name");
            }
            if (_body.contains("initial_guesses"))
            {
                const auto &guesses = _body.at("initial_guesses");
                if (!guesses.is_array() || guesses.size() != config.at("paths").size())
                    throw std::runtime_error("Placement selection changed; reopen the placement editor");
                for (const auto &pose : guesses)
                    validatePlacement(pose);
                config["initial_guesses"] = guesses;
            }
            if (_body.contains("chunk_initial_guesses"))
            {
                const auto &guesses = _body.at("chunk_initial_guesses");
                const auto directory = std::filesystem::u8path(config.at("output_path").get<std::string>()).parent_path();
                const auto manifest = readJson(directory / "partial_chunks.json");
                if (!guesses.is_object() || guesses.size() != manifest.at("chunks").size())
                    throw std::runtime_error("Chunks changed; reopen the placement editor");
                for (const auto &chunk : manifest.at("chunks"))
                {
                    const auto key = chunk.at("key").get<std::string>();
                    if (!guesses.contains(key))
                        throw std::runtime_error("Chunks changed; reopen the placement editor");
                    validatePlacement(guesses.at(key));
                }
                config.erase("initial_guesses");
                config["chunk_initial_guesses"] = guesses;
                config["manual_chunks"] = manifest.at("chunks");
            }
            config["review_only"] = _body.value("review_only", false);
            saveJson(report / "config.json", config);
            auto &job = findJob(state_, id);
            job["status"] = "running";
            job["initial_placement_count"] = 0;
            for (const auto &pose : config.value("initial_guesses", nlohmann::json::array()))
                if (!pose.is_null())
                    job["initial_placement_count"] = job["initial_placement_count"].get<int>() + 1;
            job["stage"] = "Starting registration with " + std::to_string(job["initial_placement_count"].get<int>()) + " scan initial placements";
            job["additional_pairs"] = config["additional_pairs"];
            launchLocked(id, report, {state_.at("python").get<std::string>(), "-u", (scripts_ / "studio_worker.py").u8string(), "--run", (report / "config.json").u8string(), "--report", report.u8string()});
        }

        std::filesystem::path placementScan(const std::string &_id, std::size_t _row)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            const auto &job = findJob(state_, _id);
            const auto config = readJson(std::filesystem::u8path(job.at("report_directory").get<std::string>()) / "config.json");
            if (_row >= config.at("paths").size())
                throw std::runtime_error("Unknown registration scan");
            return std::filesystem::u8path(config.at("paths").at(_row).get<std::string>());
        }

        nlohmann::json placementData(const std::string &_id)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            const auto &job = findJob(state_, _id);
            const auto root = std::filesystem::u8path(job.at("output_path").get<std::string>()).parent_path();
            const auto manifest = readJson(root / "partial_chunks.json");
            const auto config = readJson(std::filesystem::u8path(job.at("report_directory").get<std::string>()) / "config.json");
            auto items = nlohmann::json::array();
            for (const auto &chunk : manifest.at("chunks"))
            {
                auto scans = nlohmann::json::array();
                for (const auto &scan : chunk.at("scans"))
                {
                    const auto row = scan.at("row").get<std::size_t>() - 1;
                    const auto path = std::filesystem::u8path(config.at("paths").at(row).get<std::string>());
                    const auto metadata = inspectPly(path);
                    scans.push_back({{"name", scan.at("name")}, {"points", metadata.at("valid_points")}, {"base", scan.at("transform_chunk_from_scan")}, {"url", "/api/registration/placement-sample/" + _id + "/" + std::to_string(row)}});
                }
                const auto key = chunk.at("key").get<std::string>();
                auto pose = nlohmann::json();
                if (config.contains("chunk_initial_guesses") && config["chunk_initial_guesses"].contains(key))
                    pose = config["chunk_initial_guesses"][key];
                items.push_back({{"id", key}, {"name", "Chunk " + std::to_string(chunk.at("number").get<int>())}, {"scans", scans}, {"pose", pose}});
            }
            return {{"items", items}, {"busy", busy_}, {"mode", "chunks"}};
        }

        std::filesystem::path outputDirectory(const std::string &_id)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            return std::filesystem::u8path(findJob(state_, _id).at("output_path").get<std::string>()).parent_path();
        }

        std::filesystem::path reviewPath(const std::string &_id)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            const auto &job = findJob(state_, _id);
            if (job.contains("review_path"))
            {
                return std::filesystem::u8path(job.at("review_path").get<std::string>());
            }
            return std::filesystem::u8path(job.at("report_directory").get<std::string>()) / "review" / "index.html";
        }

    private:
        static nlohmann::json placementDocument(const std::filesystem::path &_file)
        {
            if (!std::filesystem::exists(_file))
                return {{"schema_version", 1}, {"kind", "scan_initial_placements"}, {"units", "metres_radians"}, {"poses", nlohmann::json::object()}};
            const auto doc = readJson(_file);
            if (doc.value("schema_version", 0) != 1 || doc.value("kind", "") != "scan_initial_placements" || !doc.at("poses").is_object())
                throw std::runtime_error("Invalid initial_placements.json; keep the file and choose another output folder");
            return doc;
        }

        static void verifySavedPlacements(const nlohmann::json &_body, const nlohmann::json &_paths, const std::filesystem::path &_directory)
        {
            if (_body.value("ignore_saved_placements", false))
            {
                for (const auto &pose : _body.value("initial_guesses", nlohmann::json::array()))
                    if (!pose.is_null())
                        throw std::runtime_error("Automatic mode conflicts with supplied placements");
                return;
            }
            const auto doc = placementDocument(_directory / "initial_placements.json");
            const auto guesses = _body.value("initial_guesses", nlohmann::json::array());
            for (std::size_t i = 0; i < _paths.size(); ++i)
            {
                const auto key = std::filesystem::weakly_canonical(std::filesystem::u8path(_paths[i].get<std::string>())).u8string();
                if (doc["poses"].contains(key) &&
                    ((i >= guesses.size() && !doc["poses"][key].is_null()) ||
                     (i < guesses.size() && guesses[i] != doc["poses"][key])))
                    throw std::runtime_error("Saved placements differ from this selection. Load saved placements or explicitly choose Clear initial placements for automatic registration. No job was started.");
            }
        }

        static void validatePairs(const std::string &_text,
                                  std::size_t _count)
        {
            std::istringstream stream(_text);
            const std::regex pattern(R"(\s*([0-9]+)\s*:\s*([0-9]+)\s*)");
            std::string item;
            while (std::getline(stream, item, ','))
            {
                if (item.find_first_not_of(" \t\r\n") == std::string::npos)
                {
                    continue;
                }
                std::smatch match;
                if (!std::regex_match(item, match, pattern))
                {
                    throw std::runtime_error("Use input row numbers for pairs, for example 1:4,2:5");
                }
                const auto first = std::stoull(match[1]);
                const auto second = std::stoull(match[2]);
                if (first < 1 || first >= second || second > _count)
                {
                    throw std::runtime_error("Each pair must contain two different input row numbers in ascending order");
                }
            }
        }

        static std::string defaultPython()
        {
#ifdef _WIN32
            return "python";
#else
            return "python3";
#endif
        }

        static nlohmann::json readJson(const std::filesystem::path &_path)
        {
#ifdef _WIN32
            // Polling must allow the worker to atomically replace its JSON files.
            const auto handle = CreateFileW(_path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
            if (handle == INVALID_HANDLE_VALUE)
                throw std::runtime_error("Cannot read registration record: " + _path.u8string());
            std::string contents;
            try
            {
                char buffer[16384];
                DWORD count = 0;
                do
                {
                    if (!ReadFile(handle, buffer, sizeof(buffer), &count, nullptr))
                        throw std::runtime_error("Cannot read registration record: " + _path.u8string());
                    contents.append(buffer, count);
                } while (count);
            }
            catch (...)
            {
                CloseHandle(handle);
                throw;
            }
            CloseHandle(handle);
            return nlohmann::json::parse(contents);
#else
            std::ifstream stream(_path);
            return nlohmann::json::parse(stream);
#endif
        }

        static void saveJson(const std::filesystem::path &_path,
                             const nlohmann::json &_value)
        {
            auto pending = _path;
            pending += ".pending";
            writeText(pending, _value.dump(2));
            replaceFile(pending, _path);
        }

        static std::string readLog(const std::filesystem::path &_path)
        {
            std::ifstream stream(_path, std::ios::binary | std::ios::ate);
            if (!stream)
            {
                return "";
            }
            const auto length = std::min<std::streamoff>(stream.tellg(), 12000);
            stream.seekg(-length, std::ios::end);
            std::string result(static_cast<std::size_t>(length), '\0');
            stream.read(result.data(), length);
            //Codex: A tail may begin in the middle of a UTF-8 character; start at the next complete line.
            if (stream.tellg() > length)
            {
                const auto newline = result.find('\n');
                result = newline == std::string::npos ? "" : result.substr(newline + 1);
            }
            return result;
        }

        static nlohmann::json &findJob(nlohmann::json &_state,
                                       const std::string &_id)
        {
            for (auto &job : _state.at("jobs"))
            {
                if (job.at("id") == _id)
                {
                    return job;
                }
            }
            throw std::runtime_error("Registration job not found");
        }

        void saveLocked()
        {
            saveJson(root_ / "registration_jobs.json", state_);
        }

        void requireIdleLocked()
        {
            if (busy_)
            {
                throw std::runtime_error("A registration operation is already running");
            }
            if (!std::filesystem::is_regular_file(scripts_ / "studio_worker.py"))
            {
                throw std::runtime_error("The mapping folder must be beside the BLK Studio executable");
            }
            if (worker_.joinable())
            {
                worker_.join();
            }
        }

        void launchLocked(const std::string &_id,
                          const std::filesystem::path &_report,
                          const std::vector<std::string> &_arguments,
                          bool _export = false)
        {
            active_id_ = _id;
            active_export_ = _export;
            cancel_.store(false);
            saveJson(_report / "progress.json", {{"status", "running"}, {"stage", "Starting worker"}});
            saveLocked();
            busy_ = true;
            const auto product_directory = _export ? std::filesystem::u8path(findJob(state_, _id).at("poses_path").get<std::string>()).parent_path() : std::filesystem::path();
            worker_ = std::thread([this, _id, _report, _arguments, _export, product_directory]
            {
                nlohmann::json result;
                try
                {
                    const int code = runWorkerProcess(_arguments, _report / "worker.log", cancel_);
                    if (_export && std::filesystem::is_regular_file(_report / "staging.json"))
                    {
                        const auto staging = std::filesystem::weakly_canonical(std::filesystem::u8path(readJson(_report / "staging.json").at("path").get<std::string>()));
                        if (staging.parent_path() == std::filesystem::canonical(product_directory) && staging.filename().u8string().rfind(".product-", 0) == 0)
                        {
                            std::filesystem::remove_all(staging);
                        }
                    }
                    result = readJson(_report / "progress.json");
                    if (code == -1)
                    {
                        result = {{"status", "cancelled"}, {"stage", _export ? "Generation stopped. Original scans, poses, and completed products are preserved." : "Stopped. Completed scan caches are retained for resume."}};
                    }
                    else if (code != 0 || (result.value("status", "") != "completed" && result.value("status", "") != "needs_connections"))
                    {
                        result["status"] = "failed";
                        if (result.value("stage", "") == "Starting worker")
                        {
                            result["stage"] = "Python could not run the worker. Check the runtime path and worker log.";
                        }
                    }
                    if (_export && result.contains("ply_path") && result.value("status", "") == "completed")
                    {
                        const std::string path = result.at("ply_path");
                        const auto imported = project_.addPointClouds({path});
                        if (!imported.at("errors").empty())
                        {
                            throw std::runtime_error("Map was written but could not be opened for viewing: " + imported.at("errors").dump());
                        }
                        const auto project = project_.snapshot();
                        for (const auto &cloud : project.at("point_clouds"))
                        {
                            if (cloud.at("path") == path)
                            {
                                result["cloud_id"] = cloud.at("id");
                            }
                        }
                    }
                }
                catch (const std::exception &error)
                {
                    result = {{"status", "failed"}, {"stage", error.what()}};
                }
                std::lock_guard<std::mutex> lock(mutex_);
                auto &job = _id.empty() ? state_["runtime"] : findJob(state_, _id);
                auto &record = _export ? job["export"] : job;
                record.update(result);
                if (_export && result.value("status", "") == "completed" && result.contains("product"))
                {
                    job["products"][result.at("product").get<std::string>()] = result;
                }
                if (_id.empty() && result.value("status", "") == "completed")
                {
                    state_["python"] = result.at("python");
                }
                try
                {
                    saveLocked();
                }
                catch (const std::exception &error)
                {
                    state_["storage_error"] = error.what();
                }
                busy_ = false;
            });
        }

        Project &project_;
        std::filesystem::path scripts_;
        std::filesystem::path root_;
        nlohmann::json state_;
        std::mutex mutex_;
        std::thread worker_;
        std::atomic<bool> cancel_{false};
        bool busy_ = false;
        bool active_export_ = false;
        std::string active_id_;
    };
} //namespace blk_core
