#pragma once

#include <atomic>
#include <chrono>
#include <filesystem>
#include <mutex>
#include <map>
#include <thread>
#include <json.hpp>
#include "project.hpp"
#include "worker_process.hpp"

namespace blk_core
{
    class TrajectoryJobs
    {
    public:
        TrajectoryJobs(Project &project, std::filesystem::path scripts):
            root_(std::filesystem::u8path(project.snapshot().at("project_directory").get<std::string>())), scripts_(std::move(scripts))
        {
            state_ = std::filesystem::is_regular_file(root_ / "trajectory_jobs.json")
                ? readJson(root_ / "trajectory_jobs.json")
                : nlohmann::json{{"jobs", nlohmann::json::array()}, {"draft", nlohmann::json::object()}};
            for (auto &job : state_["jobs"])
                if (job.value("status", "") == "running" && !job.value("managed_externally", false)) job["status"] = "interrupted";
            save();
        }

        ~TrajectoryJobs()
        {
            cancel();
            if (worker_.joinable()) worker_.join();
        }

        void cancel() { cancel_.store(true); }

        void draft(const nlohmann::json &value)
        {
            if (!value.is_object()) throw std::runtime_error("Trajectory settings must be an object");
            std::lock_guard<std::mutex> lock(mutex_);
            state_["draft"] = value;
            save();
        }

        nlohmann::json snapshot()
        {
            std::lock_guard<std::mutex> lock(mutex_);
            auto result = state_;
            result["running"] = running_;
            for (auto &job : result["jobs"])
            {
                if (job.value("managed_externally", false))
                {
                    try
                    {
                        const auto progress = readJson(std::filesystem::u8path(job.at("output_directory").get<std::string>()) / "job_status.json");
                        const auto status = progress.at("status").get<std::string>();
                        const double now = std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
                        if ((status == "running" || status == "queued" || status == "auditing") && now - progress.at("updated_unix").get<double>() > 180)
                        {
                            job["status"] = "interrupted";
                            job["stage"] = "Status updates stopped";
                        }
                        else
                        {
                            for (const auto *key : {"status", "stage", "accepted", "processed", "phase"})
                                if (progress.contains(key)) job[key] = progress[key];
                        }
                    }
                    catch (const std::exception &)
                    {
                        job["status"] = "interrupted";
                        job["stage"] = "Progress file unavailable";
                    }
                }
                const auto report = std::filesystem::u8path(job.value("report_directory", ""));
                if (!report.empty() && !job.value("managed_externally", false))
                {
                    try
                    {
                        if (job.value("status", "") == "running")
                        {
                            const auto progress = readJson(report / "progress.json");
                            for (auto &[key, value] : progress.items()) if (key != "status") job[key] = value;
                        }
                        job["log_tail"] = tail(report / "worker.log");
                    }
                    catch (...) {}
                }
                job["viewer_available"] = std::filesystem::is_regular_file(std::filesystem::u8path(job.at("output_directory").get<std::string>()) / "viewer.json");
            }
            return result;
        }

        void start(const nlohmann::json &body, const std::string &python)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (running_) throw std::runtime_error("A trajectory worker is already running");
            if (worker_.joinable()) worker_.join();
            const bool inspect = body.value("inspect", false);
            nlohmann::json config;
            std::string id;
            if (body.contains("resume_id"))
            {
                auto &job = find(body.at("resume_id"));
                if (job.value("managed_externally", false)) throw std::runtime_error("This job is managed by its existing worker");
                config = job.at("config");
                id = job.at("id");
            }
            else
            {
                config = body.at("config");
                const auto output = config.at("output_directory").get<std::string>();
                if (output.empty()) throw std::runtime_error("Choose a trajectory output folder");
                config["output_directory"] = std::filesystem::weakly_canonical(std::filesystem::absolute(std::filesystem::u8path(output))).u8string();
                id = newId();
                state_["jobs"].push_back({{"id", id}, {"name", config.value("name", "Trajectory")}, {"config", config},
                    {"output_directory", config.at("output_directory")}, {"inspect", inspect}});
            }
            const auto report = root_ / "trajectory_reports" / newId();
            std::filesystem::create_directories(report);
            writeText(report / "config.json", config.dump(2));
            writeText(report / "progress.json", nlohmann::json{{"status", "running"}, {"stage", "Starting trajectory worker"}}.dump());
            auto &job = find(id);
            job["report_directory"] = report.u8string();
            job["status"] = "running";
            job["stage"] = inspect ? "Checking inputs" : "Starting trajectory generation";
            state_["draft"] = config;
            save();
            std::vector<std::string> args{python, "-u", (scripts_ / "trajectory_worker.py").u8string(),
                "--config", (report / "config.json").u8string(), "--report", (report / "progress.json").u8string()};
            if (inspect) args.push_back("--inspect");
            cancel_.store(false);
            running_ = true;
            worker_ = std::thread([this, id, args, report]
            {
                nlohmann::json result;
                try
                {
                    const int code = runWorkerProcess(args, report / "worker.log", cancel_);
                    result = readJson(report / "progress.json");
                    if (code == -1) result = {{"status", "cancelled"}, {"stage", "Stopped; completed work is available for resume"}};
                    else if (code != 0) result["status"] = "failed";
                }
                catch (const std::exception &error) { result = {{"status", "failed"}, {"stage", error.what()}}; }
                std::lock_guard<std::mutex> guard(mutex_);
                find(id).update(result);
                running_ = false;
                try { save(); } catch (...) {}
            });
        }

        void open(const std::string &directory)
        {
            const auto path = std::filesystem::canonical(std::filesystem::u8path(directory));
            const auto viewer = readJson(path / "viewer.json");
            const auto provenance = readJson(path / "provenance.json");
            if (viewer.value("schema_version", 0) != 1 || !viewer.at("maps").is_array())
                throw std::runtime_error("Not a BLK Studio trajectory result");
            std::lock_guard<std::mutex> lock(mutex_);
            for (const auto &job : state_["jobs"])
                if (!job.value("inspect", false) && job.at("output_directory") == path.u8string()) return;
            state_["jobs"].push_back({{"id", newId()}, {"name", viewer.value("name", "Trajectory")},
                {"output_directory", path.u8string()}, {"config", provenance.at("config")}, {"status", "saved"}, {"stage", "Saved result available"},
                {"managed_externally", std::filesystem::is_regular_file(path / "job_status.json")}});
            save();
        }

        void monitor(const nlohmann::json &config)
        {
            const auto path = std::filesystem::canonical(std::filesystem::u8path(config.at("output_directory").get<std::string>()));
            const auto progress = readJson(path / "job_status.json");
            if (!progress.at("status").is_string() || !progress.at("updated_unix").is_number())
                throw std::runtime_error("Invalid trajectory progress file");
            std::lock_guard<std::mutex> lock(mutex_);
            for (auto &job : state_["jobs"])
                if (!job.value("inspect", false) && job.at("output_directory") == path.u8string())
                {
                    if (job.value("status", "") == "running" && !job.value("managed_externally", false))
                        throw std::runtime_error("This output already has an app-managed worker");
                    job["managed_externally"] = true;
                    save();
                    return;
                }
            state_["jobs"].push_back({{"id", newId()}, {"name", config.value("name", "Trajectory")},
                {"output_directory", path.u8string()}, {"config", config}, {"status", "queued"}, {"managed_externally", true}});
            save();
        }

        nlohmann::json viewer(const std::string &id)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            return readJson(std::filesystem::u8path(find(id).at("output_directory").get<std::string>()) / "viewer.json");
        }

        std::string outputDirectory(const std::string &id)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            return find(id).at("output_directory").get<std::string>();
        }

        std::filesystem::path map(const std::string &id, const std::string &key)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            const auto path = std::filesystem::u8path(find(id).at("output_directory").get<std::string>()) / "viewer.json";
            const auto modified = std::filesystem::last_write_time(path);
            if (!map_cache_.count(id) || map_cache_times_[id] != modified)
            {
                map_cache_[id] = readJson(path).at("maps");
                map_cache_times_[id] = modified;
            }
            for (const auto &item : map_cache_.at(id))
                if (item.at("key") == key) return std::filesystem::u8path(item.at("path").get<std::string>());
            throw std::runtime_error("Unknown trajectory map layer");
        }

    private:
        static nlohmann::json readJson(const std::filesystem::path &path)
        {
            std::ifstream stream(path);
            if (!stream) throw std::runtime_error("Cannot read " + path.u8string());
            return nlohmann::json::parse(stream);
        }
        static std::string tail(const std::filesystem::path &path)
        {
            std::ifstream stream(path, std::ios::binary);
            if (!stream) return "";
            stream.seekg(0, std::ios::end);
            const auto size = stream.tellg();
            stream.seekg(std::max<std::streamoff>(0, static_cast<std::streamoff>(size) - 8000));
            return std::string(std::istreambuf_iterator<char>(stream), {});
        }
        nlohmann::json &find(const std::string &id)
        {
            for (auto &job : state_["jobs"]) if (job.at("id") == id) return job;
            throw std::runtime_error("Unknown trajectory job");
        }
        void save() { writeText(root_ / "trajectory_jobs.json", state_.dump(2)); }
        std::filesystem::path root_, scripts_;
        nlohmann::json state_;
        std::mutex mutex_;
        std::thread worker_;
        std::atomic<bool> cancel_{false};
        bool running_ = false;
        std::map<std::string, nlohmann::json> map_cache_;
        std::map<std::string, std::filesystem::file_time_type> map_cache_times_;
    };
}
