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
#include "worker_log.hpp"

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
                if ((job.value("status", "") == "running" || job.value("status", "") == "queued") && !job.value("managed_externally", false)) job["status"] = "interrupted";
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
                        job["log_url"] = "trajectory/" + job.at("id").get<std::string>() + "/log";
                    }
                    catch (...) {}
                }
                job["viewer_available"] = std::filesystem::is_regular_file(viewerPath(job));
            }
            return result;
        }

        nlohmann::json logPage(const std::string &id, const std::string &before, const std::string &after)
        {
            std::filesystem::path path;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                const auto report = find(id).value("report_directory", "");
                if (!report.empty()) path = std::filesystem::u8path(report) / "worker.log";
            }
            return workerLogPage(path, before, after);
        }

        nlohmann::json helper(nlohmann::json request, const std::string &python)
        {
            const auto temporary = root_ / "trajectory_requests" / newId();
            std::filesystem::create_directories(temporary);
            try
            {
                writeText(temporary / "request.json", request.dump());
                std::atomic<bool> cancelled{false};
                const int code = runWorkerProcess({python, "-u", (scripts_ / "trajectory_project.py").u8string(),
                    "--request", (temporary / "request.json").u8string(), "--response", (temporary / "response.json").u8string()},
                    temporary / "worker.log", cancelled);
                if (code != 0) throw std::runtime_error("Could not read trajectory files: " + tail(temporary / "worker.log"));
                const auto response = readJson(temporary / "response.json");
                std::filesystem::remove_all(temporary);
                if (response.contains("error")) throw std::runtime_error(response.at("error").get<std::string>());
                return response;
            }
            catch (...) { std::filesystem::remove_all(temporary); throw; }
        }

        nlohmann::json start(const nlohmann::json &body, const std::string &python)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (running_) throw std::runtime_error("A trajectory batch is already running");
            if (worker_.joinable()) worker_.join();
            const bool inspect = body.value("inspect", false);
            auto configs = body.contains("configs") ? body.at("configs") : nlohmann::json::array();
            std::string resume;
            if (body.contains("resume_id"))
            {
                resume = body.at("resume_id");
                auto &job = find(resume);
                if (job.value("managed_externally", false)) throw std::runtime_error("This job is managed by its existing worker");
                configs.push_back(job.at("config"));
            }
            else if (configs.empty() && body.contains("config")) configs.push_back(body.at("config"));
            if (!configs.is_array() || configs.empty()) throw std::runtime_error("Add at least one sequence");
            // An explicit batch shares one CPU budget; single-job resume keeps its saved settings.
            auto budget = body.contains("configs") ? helper({{"operation","budget"},{"configs",configs},
                {"workers",body.value("workers",0)}}, python) : nlohmann::json{{"concurrency",1}};
            std::vector<std::string> ids;
            std::vector<std::vector<std::string>> commands;
            std::vector<std::filesystem::path> reports;
            for (auto config : configs)
            {
                auto output = config.at("output_directory").get<std::string>();
                if (output.empty()) throw std::runtime_error("Choose a sequence output folder");
                output = std::filesystem::weakly_canonical(std::filesystem::absolute(std::filesystem::u8path(output))).u8string();
                config["output_directory"] = output;
                if (budget.contains("workers_per_sequence")) config["workers"] = budget.at("workers_per_sequence");
                const auto id = resume.empty() ? newId() : resume;
                if (resume.empty()) state_["jobs"].push_back({{"id",id},{"name",config.value("name","Trajectory")},
                    {"config",config},{"output_directory",output},{"inspect",inspect}});
                const auto report = root_ / "trajectory_reports" / newId();
                std::filesystem::create_directories(report);
                writeText(report / "config.json", config.dump(2));
                writeText(report / "progress.json", nlohmann::json{{"status","queued"},{"stage","Waiting for a batch worker"}}.dump());
                auto &job = find(id);
                job["report_directory"] = report.u8string(); job["status"] = "queued";
                job["stage"] = "Waiting for a batch worker";
                ids.push_back(id); reports.push_back(report);
                commands.push_back({python,"-u",(scripts_/"trajectory_worker.py").u8string(),
                    "--config",(report/"config.json").u8string(),"--report",(report/"progress.json").u8string()});
                if (inspect) commands.back().push_back("--inspect");
            }
            if (body.contains("config")) state_["draft"] = body.at("config");
            save(); cancel_.store(false); running_ = true;
            const auto concurrency = std::min(ids.size(), budget.at("concurrency").get<std::size_t>());
            worker_ = std::thread([this, ids, commands, reports, concurrency, inspect]
            {
                std::atomic<std::size_t> next{0};
                std::vector<std::thread> lanes;
                for (std::size_t lane=0; lane<concurrency; ++lane) lanes.emplace_back([&]
                {
                    while (!cancel_.load())
                    {
                        const auto i=next.fetch_add(1);
                        if (i>=ids.size()) break;
                        {
                            std::lock_guard<std::mutex> guard(mutex_);
                            auto &job=find(ids[i]); job["status"]="running";
                            job["stage"]=inspect ? "Checking inputs" : "Generating ground-truth trajectory";
                            save();
                        }
                        nlohmann::json result;
                        try
                        {
                            const int code=runWorkerProcess(commands[i],reports[i]/"worker.log",cancel_);
                            result=readJson(reports[i]/"progress.json");
                            if (code==-1) result={{"status","cancelled"},{"stage","Stopped; completed work is available for resume"}};
                            else if (code!=0) result["status"]="failed";
                        }
                        catch (const std::exception &error) { result={{"status","failed"},{"stage",error.what()}}; }
                        std::lock_guard<std::mutex> guard(mutex_);
                        find(ids[i]).update(result);
                        try { save(); } catch (...) {}
                    }
                });
                for (auto &lane:lanes) lane.join();
                std::lock_guard<std::mutex> guard(mutex_);
                for (const auto &id:ids) if (find(id).value("status","")=="queued")
                    find(id).update({{"status","cancelled"},{"stage","Batch stopped before launch"}});
                running_=false;
                try { save(); } catch (...) {}
            });
            return {{"ok",true},{"ids",ids},{"concurrency",concurrency}};
        }

        nlohmann::json open(const std::vector<std::string> &paths, const std::string &python)
        {
            const auto results=helper({{"operation","open"},{"paths",paths}},python).at("results");
            std::lock_guard<std::mutex> lock(mutex_);
            std::vector<std::string> ids;
            for (const auto &result:results)
            {
                std::string existing;
                for (auto &job:state_["jobs"])
                    if (!job.value("inspect",false) && job.at("output_directory")==result.at("output_directory") &&
                        job.value("source_file","")==result.value("source_file","")) existing=job.at("id").get<std::string>();
                const auto id=existing.empty() ? newId() : existing;
                if (existing.empty()) state_["jobs"].push_back({{"id",id},{"name",result.at("name")},
                    {"output_directory",result.at("output_directory")},{"config",nlohmann::json::object()},
                    {"status","saved"},{"stage","Saved result available"}});
                auto &job=find(id);
                if (result.contains("scene"))
                {
                    const auto folder=root_/"trajectory_reports"/id;
                    std::filesystem::create_directories(folder);
                    writeText(folder/"viewer.json",result.at("scene").dump());
                    job["viewer_path"]=(folder/"viewer.json").u8string();
                    job["source_file"]=result.at("source_file");
                }
                else
                {
                    const auto folder=std::filesystem::u8path(result.at("output_directory").get<std::string>());
                    if (std::filesystem::is_regular_file(folder/"provenance.json"))
                        job["config"]=readJson(folder/"provenance.json").at("config");
                    job["managed_externally"]=std::filesystem::is_regular_file(folder/"job_status.json");
                }
                ids.push_back(id);
            }
            save();
            return {{"ok",true},{"ids",ids}};
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
            return readJson(viewerPath(find(id)));
        }

        std::string outputDirectory(const std::string &id)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            const auto &job=find(id);
            if (job.contains("config") && job.at("config").contains("sequence_directory"))
                return job.at("config").at("sequence_directory").get<std::string>();
            return job.at("output_directory").get<std::string>();
        }

        std::filesystem::path map(const std::string &id, const std::string &key)
        {
            std::lock_guard<std::mutex> lock(mutex_);
            const auto path = viewerPath(find(id));
            const auto modified = std::filesystem::last_write_time(path);
            if (!map_cache_.count(id) || map_cache_times_[id] != modified)
            {
                map_cache_[id] = readJson(path).at("maps");
                map_cache_times_[id] = modified;
            }
            for (const auto &item : map_cache_.at(id))
                if (item.at("key") == key) return path.parent_path() / std::filesystem::u8path(item.at("path").get<std::string>());
            throw std::runtime_error("Unknown trajectory map layer");
        }

    private:
        static std::filesystem::path viewerPath(const nlohmann::json &job)
        {
            if (job.contains("viewer_path")) return std::filesystem::u8path(job.at("viewer_path").get<std::string>());
            return std::filesystem::u8path(job.at("output_directory").get<std::string>())/"viewer.json";
        }
        static nlohmann::json readJson(const std::filesystem::path &path)
        {
            std::ifstream stream(path);
            if (!stream) throw std::runtime_error("Cannot read " + path.u8string());
            return nlohmann::json::parse(stream);
        }
        static std::string tail(const std::filesystem::path &path)
        {
            return workerLogPage(path, "", "", 8000).at("text").get<std::string>();
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
