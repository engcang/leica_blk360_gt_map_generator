#pragma once

#include <algorithm>
#include <atomic>
#include <cctype>
#include <condition_variable>
#include <cstdint>
#include <filesystem>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <json.hpp>

#include "converter.hpp"
#include "platform.hpp"
#include "ply.hpp"

namespace blk_core
{
    inline bool isBlk(const std::filesystem::path &_path)
    {
        auto extension = _path.extension().u8string();
        std::transform(extension.begin(), extension.end(), extension.begin(), [](unsigned char _value)
        {
            return static_cast<char>(std::tolower(_value));
        });
        return extension == ".blk";
    }

    class Project
    {
    public:
        explicit Project(std::filesystem::path _root):
            root_(std::move(_root))
        {
            std::filesystem::create_directories(root_);
            state_ = {{"schema_version", 2}, {"name", "My scan project"}, {"units", "metres"}, {"output_directory", ""}, {"jobs", nlohmann::json::array()}, {"registration", {{"status", "not_started"}, {"edges", nlohmann::json::array()}, {"map_frame", "map"}}}};
            if (std::filesystem::exists(root_ / "project.json"))
            {
                state_ = nlohmann::json::parse(readText(root_ / "project.json"));
                if (state_.value("schema_version", 0) == 1)
                {
                    migrateLegacy();
                }
                if (state_.value("schema_version", 0) != 2 || !state_.contains("jobs") || !state_["jobs"].is_array())
                {
                    throw std::runtime_error("Unsupported project file");
                }
                for (auto &job : state_["jobs"])
                {
                    if (!validId(job.value("id", "")))
                    {
                        throw std::runtime_error("Invalid job ID in project");
                    }
                    if (job["status"] == "running" || job["status"] == "queued")
                    {
                        job["status"] = "interrupted";
                        job["message"] = "Previous session ended. Check the output folder before retrying.";
                    }
                }
            }
            if (!state_.contains("point_clouds"))
            {
                state_["point_clouds"] = nlohmann::json::array();
            }
            if (!state_["point_clouds"].is_array())
            {
                throw std::runtime_error("Invalid point-cloud library");
            }
            for (const auto &cloud : state_["point_clouds"])
            {
                if (!validId(cloud.value("id", "")) || !cloud.contains("path") || !cloud["path"].is_string())
                {
                    throw std::runtime_error("Invalid saved point-cloud record");
                }
            }
            refreshCloudsLocked();
            saveLocked();
            worker_ = std::thread([this]
            {
                workLoop();
            });
        }
        Project(const Project &) = delete;
        Project &operator=(const Project &) = delete;
        ~Project()
        {
            {
                std::lock_guard<std::mutex> lock(mutex_);
                stopping_ = true;
                cancel_.store(true);
            }
            condition_.notify_all();
            if (worker_.joinable())
            {
                worker_.join();
            }
        }
        static bool validId(const std::string &_id)
        {
            return _id.size() == 32 && _id.find_first_not_of("0123456789abcdef") == std::string::npos;
        }
        nlohmann::json snapshot()
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (pruneMissingCloudsLocked())
            {
                saveLocked();
            }
            auto result = state_;
            result["project_directory"] = root_.u8string();
            result["running"] = !active_id_.empty();
            return result;
        }
        nlohmann::json scan(const std::string &_id) const
        {
            std::lock_guard<std::mutex> lock(mutex_);
            for (const auto &job : state_["jobs"])
            {
                if (job["id"] == _id)
                {
                    return job;
                }
            }
            throw std::runtime_error("Unknown scan");
        }
        std::filesystem::path outputPath(const std::string &_id) const
        {
            const auto job = scan(_id);
            if (job["status"] != "completed")
            {
                throw std::runtime_error("Scan has not completed conversion");
            }
            return std::filesystem::u8path(job.at("output_path").get<std::string>());
        }
        nlohmann::json pointCloud(const std::string &_id) const
        {
            std::lock_guard<std::mutex> lock(mutex_);
            for (const auto &cloud : state_["point_clouds"])
            {
                if (cloud["id"] == _id)
                {
                    auto result = cloud;
                    const auto metadata = inspectPly(std::filesystem::u8path(cloud.at("path").get<std::string>()));
                    result.update(metadata);
                    return result;
                }
            }
            throw std::runtime_error("Unknown saved point cloud");
        }
        nlohmann::json addPointClouds(const std::vector<std::string> &_paths)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            std::size_t added = 0, skipped = 0;
            auto errors = nlohmann::json::array();
            for (const auto &path : _paths)
            {
                try
                {
                    if (addCloudLocked(std::filesystem::u8path(path)))
                    {
                        ++added;
                    }
                    else
                    {
                        ++skipped;
                    }
                }
                catch (const std::exception &error)
                {
                    errors.push_back({{"path", path}, {"error", error.what()}});
                }
            }
            saveLocked();
            return {{"added", added}, {"skipped", skipped}, {"errors", errors}};
        }
        void refreshClouds()
        {
            std::lock_guard<std::mutex> lock(mutex_);
            refreshCloudsLocked();
            saveLocked();
        }
        nlohmann::json addPaths(const std::vector<std::string> &_paths)
        {
            std::vector<std::filesystem::path> candidates;
            auto errors = nlohmann::json::array();
            for (const auto &text : _paths)
            {
                try
                {
                    const auto path = std::filesystem::canonical(std::filesystem::u8path(text));
                    if (std::filesystem::is_directory(path))
                    {
                        for (const auto &entry : std::filesystem::recursive_directory_iterator(path, std::filesystem::directory_options::skip_permission_denied))
                        {
                            if (entry.is_regular_file() && isBlk(entry.path()))
                            {
                                candidates.push_back(entry.path());
                            }
                        }
                    }
                    else if (isBlk(path))
                    {
                        candidates.push_back(path);
                    }
                    else
                    {
                        throw std::runtime_error("Select a BLK file or a folder containing BLK files");
                    }
                }
                catch (const std::exception &error)
                {
                    errors.push_back({{"path", text}, {"error", error.what()}});
                }
            }
            std::sort(candidates.begin(), candidates.end());
            std::size_t added = 0, skipped = 0;
            for (const auto &candidate : candidates)
            {
                try
                {
                    const auto path = std::filesystem::canonical(candidate);
                    BlkContainer(path).parse();
                    std::lock_guard<std::mutex> lock(mutex_);
                    bool duplicate = false;
                    for (const auto &job : state_["jobs"])
                    {
                        const auto existing = std::filesystem::u8path(job.at("source_path").get<std::string>());
                        std::error_code error;
                        if (existing == path || std::filesystem::equivalent(existing, path, error))
                        {
                            duplicate = true;
                            break;
                        }
                    }
                    if (duplicate)
                    {
                        ++skipped;
                        continue;
                    }
                    if (state_.value("output_directory", "").empty())
                    {
                        state_["output_directory"] = (path.parent_path() / "output").u8string();
                        refreshCloudsLocked();
                    }
                    state_["jobs"].push_back({{"id", newId()}, {"name", path.filename().u8string()}, {"source_path", path.u8string()}, {"bytes", std::filesystem::file_size(path)}, {"status", "ready"}, {"message", "Ready"}, {"progress", 0.0}, {"output_path", ""}, {"result", nullptr}});
                    saveLocked();
                    ++added;
                }
                catch (const std::exception &error)
                {
                    errors.push_back({{"path", candidate.u8string()}, {"error", error.what()}});
                }
            }
            return {{"added", added}, {"skipped", skipped}, {"errors", errors}};
        }
        void setOutput(const std::string &_path)
        {
            if (_path.empty())
            {
                throw std::runtime_error("Choose an output folder");
            }
            const auto path = std::filesystem::weakly_canonical(std::filesystem::absolute(std::filesystem::u8path(_path)));
            if (std::filesystem::exists(path) && !std::filesystem::is_directory(path))
            {
                throw std::runtime_error("Output path must be a folder");
            }
            std::lock_guard<std::mutex> lock(mutex_);
            state_["output_directory"] = path.u8string();
            refreshCloudsLocked();
            saveLocked();
        }
        void queue()
        {
            std::lock_guard<std::mutex> lock(mutex_);
            const auto output_text = state_.value("output_directory", "");
            if (output_text.empty())
            {
                throw std::runtime_error("Choose an output folder first");
            }
            const auto output = std::filesystem::u8path(output_text);
            std::filesystem::create_directories(output);
            std::vector<std::filesystem::path> reserved;
            for (const auto &job : state_["jobs"])
            {
                if (job["status"] == "running" || job["status"] == "queued")
                {
                    reserved.push_back(std::filesystem::u8path(job.at("output_path").get<std::string>()));
                }
            }
            std::vector<std::pair<std::string, std::filesystem::path>> pending;
            for (const auto &job : state_["jobs"])
            {
                if (job["status"] != "ready")
                {
                    continue;
                }
                const auto source = std::filesystem::u8path(job.at("source_path").get<std::string>());
                if (!std::filesystem::is_regular_file(source))
                {
                    throw std::runtime_error("Source is missing: " + source.u8string());
                }
                auto target = output / source.filename();
                target.replace_extension(".ply");
                if (std::filesystem::exists(target))
                {
                    throw std::runtime_error("Output already exists: " + target.u8string() + ". Open it from Saved point clouds without converting again, or choose another output folder.");
                }
                for (const auto &other : reserved)
                {
                    auto left = target.u8string(), right = other.u8string();
#ifdef _WIN32
                    std::transform(left.begin(), left.end(), left.begin(), [](unsigned char _c)
                    {
                        return static_cast<char>(std::tolower(_c));
                    });
                    std::transform(right.begin(), right.end(), right.begin(), [](unsigned char _c)
                    {
                        return static_cast<char>(std::tolower(_c));
                    });
#endif
                    if (left == right)
                    {
                        throw std::runtime_error("Two scans would produce " + target.filename().u8string() + ". Convert them into separate folders.");
                    }
                }
                reserved.push_back(target);
                pending.emplace_back(job.at("id").get<std::string>(), target);
            }
            for (const auto &entry : pending)
            {
                auto &job = findLocked(entry.first);
                job["output_path"] = entry.second.u8string();
                job["status"] = "queued";
                job["message"] = "Waiting";
            }
            saveLocked();
            condition_.notify_all();
        }
        void retry(const std::string &_id)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            auto &job = findLocked(_id);
            if (job["status"] != "failed" && job["status"] != "cancelled" && job["status"] != "interrupted")
            {
                throw std::runtime_error("Only failed, cancelled or interrupted jobs can be retried");
            }
            if (!std::filesystem::is_regular_file(std::filesystem::u8path(job.at("source_path").get<std::string>())))
            {
                throw std::runtime_error("Source BLK is missing. Remove this job and select the file in its new location.");
            }
            job["status"] = "ready";
            job["message"] = "Ready to retry";
            job["progress"] = 0.0;
            saveLocked();
        }
        void remove(const std::string &_id)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            const auto &job = findLocked(_id);
            if (job["status"] == "running" || job["status"] == "queued")
            {
                throw std::runtime_error("Stop this job before removing it");
            }
            auto &jobs = state_["jobs"];
            jobs.erase(std::remove_if(jobs.begin(), jobs.end(), [&_id](const nlohmann::json &_job)
            {
                return _job["id"] == _id;
            }),
                       jobs.end());
            saveLocked();
        }
        void cancelAll()
        {
            std::lock_guard<std::mutex> lock(mutex_);
            cancel_.store(true);
            for (auto &job : state_["jobs"])
            {
                if (job["status"] == "queued")
                {
                    job["status"] = "cancelled";
                    job["message"] = "Cancelled before conversion";
                }
            }
            saveLocked();
        }
        void rename(const std::string &_name)
        {
            if (_name.empty() || _name.size() > 200)
            {
                throw std::runtime_error("Project name must contain 1-200 bytes");
            }
            std::lock_guard<std::mutex> lock(mutex_);
            state_["name"] = _name;
            saveLocked();
        }

    private:
        bool addCloudLocked(const std::filesystem::path &_path)
        {
            const auto path = std::filesystem::canonical(_path);
            const auto metadata = inspectPly(path);
            for (auto &cloud : state_["point_clouds"])
            {
                std::error_code error;
                const auto existing = std::filesystem::u8path(cloud.at("path").get<std::string>());
                if (existing == path || std::filesystem::equivalent(existing, path, error))
                {
                    cloud.update(metadata);
                    cloud["available"] = true;
                    cloud["error"] = "";
                    return false;
                }
            }
            auto cloud = metadata;
            cloud.update({{"id", newId()}, {"name", path.filename().u8string()}, {"path", path.u8string()}, {"available", true}, {"error", ""}});
            state_["point_clouds"].push_back(cloud);
            return true;
        }
        bool pruneMissingCloudsLocked()
        {
            bool changed = false;
            for (const auto *field : {"point_clouds", "point_cloud_errors"})
            {
                if (!state_.contains(field) || !state_[field].is_array())
                {
                    continue;
                }
                auto &entries = state_[field];
                const auto previous_count = entries.size();
                entries.erase(std::remove_if(entries.begin(), entries.end(), [](const nlohmann::json &_entry)
                {
                    std::error_code error;
                    const auto path = std::filesystem::u8path(_entry.at("path").get<std::string>());
                    const bool exists = std::filesystem::exists(path, error);
                    return !exists && !error;
                }),
                              entries.end());
                changed = changed || entries.size() != previous_count;
            }
            return changed;
        }
        void refreshCloudsLocked()
        {
            pruneMissingCloudsLocked();
            for (auto &cloud : state_["point_clouds"])
            {
                try
                {
                    cloud.update(inspectPly(std::filesystem::u8path(cloud.at("path").get<std::string>())));
                    cloud["available"] = true;
                    cloud["error"] = "";
                }
                catch (const std::exception &error)
                {
                    cloud["available"] = false;
                    cloud["error"] = error.what();
                }
            }
            std::vector<std::filesystem::path> candidates;
            state_["point_cloud_errors"] = nlohmann::json::array();
            const auto output = std::filesystem::u8path(state_.value("output_directory", ""));
            std::error_code directory_error;
            if (!output.empty() && std::filesystem::is_directory(output, directory_error))
            {
                try
                {
                    for (const auto &entry : std::filesystem::directory_iterator(output, std::filesystem::directory_options::skip_permission_denied))
                    {
                        if (hasExtension(entry.path(), ".ply"))
                        {
                            candidates.push_back(entry.path());
                        }
                    }
                }
                catch (const std::exception &error)
                {
                    state_["point_cloud_errors"].push_back({{"path", output.u8string()}, {"error", error.what()}});
                }
            }
            for (const auto &job : state_["jobs"])
            {
                if (job["status"] == "completed" && !job.value("output_path", "").empty())
                {
                    candidates.push_back(std::filesystem::u8path(job.at("output_path").get<std::string>()));
                }
            }
            std::sort(candidates.begin(), candidates.end());
            candidates.erase(std::unique(candidates.begin(), candidates.end()), candidates.end());
            for (const auto &path : candidates)
            {
                std::error_code existence_error;
                if (!std::filesystem::exists(path, existence_error) && !existence_error)
                {
                    continue;
                }
                try
                {
                    addCloudLocked(path);
                }
                catch (const std::exception &error)
                {
                    state_["point_cloud_errors"].push_back({{"path", path.u8string()}, {"error", error.what()}});
                }
            }
        }
        void migrateLegacy()
        {
            std::filesystem::copy_file(root_ / "project.json", root_ / "project-v1.json", std::filesystem::copy_options::skip_existing);
            for (auto &job : state_["jobs"])
            {
                if (!validId(job.value("id", "")))
                {
                    throw std::runtime_error("Invalid legacy scan ID");
                }
                const std::string id = job["id"];
                job["source_path"] = (root_ / "inputs" / (id + ".blk")).u8string();
                job["output_path"] = (root_ / "scans" / id / "cloud.ply").u8string();
                job["legacy_storage"] = true;
                job.erase("preview_points");
                if (job["result"].is_object())
                {
                    job["result"].erase("preview_points");
                }
            }
            state_["schema_version"] = 2;
            state_["output_directory"] = "";
            state_["migration_notice"] = "Previous-version files remain at their recorded paths. Select original BLK files for new conversions; no new source copies are created.";
        }
        nlohmann::json &findLocked(const std::string &_id)
        {
            for (auto &job : state_["jobs"])
            {
                if (job["id"] == _id)
                {
                    return job;
                }
            }
            throw std::runtime_error("Unknown scan");
        }
        void saveLocked() const
        {
            const auto pending = root_ / "project.json.pending";
            writeText(pending, state_.dump(2) + "\n");
            const auto target = root_ / "project.json";
            if (std::filesystem::exists(target))
            {
                std::filesystem::copy_file(target, root_ / "project.json.backup", std::filesystem::copy_options::overwrite_existing);
            }
            replaceFile(pending, target);
        }
        void workLoop()
        {
            while (true)
            {
                std::string id;
                std::filesystem::path source, output;
                {
                    std::unique_lock<std::mutex> lock(mutex_);
                    condition_.wait(lock, [this]
                    {
                        if (stopping_)
                        {
                            return true;
                        }
                        for (const auto &job : state_["jobs"])
                        {
                            if (job["status"] == "queued")
                            {
                                return true;
                            }
                        }
                        return false;
                    });
                    if (stopping_)
                    {
                        return;
                    }
                    for (auto &job : state_["jobs"])
                    {
                        if (job["status"] != "queued")
                        {
                            continue;
                        }
                        id = job["id"];
                        source = std::filesystem::u8path(job.at("source_path").get<std::string>());
                        output = std::filesystem::u8path(job.at("output_path").get<std::string>());
                        job["status"] = "running";
                        job["message"] = "Starting";
                        break;
                    }
                    active_id_ = id;
                    cancel_.store(false);
                    try
                    {
                        saveLocked();
                    }
                    catch (const std::exception &error)
                    {
                        state_["storage_error"] = error.what();
                        cancel_.store(true);
                    }
                }
                try
                {
                    auto result = Converter().convert(source, output, cancel_, [this, &id](const Progress &_progress)
                    {
                        std::lock_guard<std::mutex> lock(mutex_);
                        auto &job = findLocked(id);
                        job["progress"] = _progress.fraction_;
                        job["message"] = _progress.stage_;
                    });
                    std::lock_guard<std::mutex> lock(mutex_);
                    auto &job = findLocked(id);
                    job["result"] = result;
                    job["source_sha256"] = result["source_sha256"];
                    job["bytes"] = result["source_bytes"];
                    job["status"] = "completed";
                    job["message"] = "Completed";
                    job["progress"] = 1.0;
                }
                catch (const std::exception &error)
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    auto &job = findLocked(id);
                    job["status"] = dynamic_cast<const Cancelled *>(&error) ? "cancelled" : "failed";
                    job["message"] = error.what();
                }
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    active_id_.clear();
                    try
                    {
                        refreshCloudsLocked();
                        saveLocked();
                    }
                    catch (const std::exception &error)
                    {
                        state_["storage_error"] = error.what();
                    }
                }
            }
        }
        std::filesystem::path root_;
        nlohmann::json state_;
        mutable std::mutex mutex_;
        std::condition_variable condition_;
        std::thread worker_;
        std::atomic<bool> cancel_{false};
        bool stopping_ = false;
        std::string active_id_;
    };
} //namespace blk_core
