#pragma once

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <exception>
#include <filesystem>
#include <fstream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <httplib.h>
#include <json.hpp>
#include <picosha2.h>

#include "file_browser.hpp"
#include "platform.hpp"
#include "project.hpp"
#include "registration.hpp"

namespace blk_core
{
    class WebServer
    {
    public:
        WebServer(std::filesystem::path _project_root,
                  std::filesystem::path _web_root):
            project_(std::move(_project_root)), web_root_(std::move(_web_root)), registration_(project_, web_root_.parent_path() / "mapping"), token_(newId() + newId())
        {
            server_.set_payload_max_length(1024 * 1024);
            server_.set_read_timeout(300);
            server_.set_write_timeout(300);
            server_.set_exception_handler([](const httplib::Request &, httplib::Response &_response, std::exception_ptr _exception)
            {
                try
                {
                    std::rethrow_exception(_exception);
                }
                catch (const std::exception &error)
                {
                    reply(_response, {{"error", error.what()}}, 400);
                }
            });
            server_.set_pre_routing_handler([this](const httplib::Request &_request, httplib::Response &_response)
            {
                const auto host = _request.get_header_value("Host");
                if (host != "127.0.0.1:" + std::to_string(port_))
                {
                    reply(_response, {{"error", "Invalid host"}}, 403);
                    return httplib::Server::HandlerResponse::Handled;
                }
                const auto origin = _request.get_header_value("Origin");
                if (!origin.empty() && origin != "http://" + host)
                {
                    reply(_response, {{"error", "Cross-origin requests are not allowed"}}, 403);
                    return httplib::Server::HandlerResponse::Handled;
                }
                if (_request.path.rfind("/api/", 0) == 0 || _request.path.rfind("/files/", 0) == 0)
                {
                    const auto supplied = _request.has_header("X-Session-Token") ? _request.get_header_value("X-Session-Token") : _request.get_param_value("token");
                    if (supplied != token_ && _request.path.rfind("/files/result/" + token_ + "/", 0) != 0)
                    {
                        reply(_response, {{"error", "Invalid session; reopen BLK Studio"}}, 403);
                        return httplib::Server::HandlerResponse::Handled;
                    }
                }
                _response.set_header("Cache-Control", "no-store");
                _response.set_header("X-Content-Type-Options", "nosniff");
                _response.set_header("Referrer-Policy", "no-referrer");
                return httplib::Server::HandlerResponse::Unhandled;
            });
            routes();
        }

        int run(bool _open_browser)
        {
            port_ = server_.bind_to_any_port("127.0.0.1");
            if (port_ <= 0)
            {
                throw std::runtime_error("Cannot bind a local port");
            }
            const auto root = std::filesystem::u8path(project_.snapshot()["project_directory"].get<std::string>());
            const auto url = "http://127.0.0.1:" + std::to_string(port_) + "/#" + token_;
            writeText(root / "session.json", nlohmann::json({{"port", port_}, {"token", token_}}).dump());
            std::thread monitor([this, _open_browser, url]
            {
                while (!server_.is_running() && !shutdown_.load())
                {
                    std::this_thread::sleep_for(std::chrono::milliseconds(20));
                }
                if (_open_browser && !shutdown_.load())
                {
                    try
                    {
                        openLocalTarget(url);
                    }
                    catch (...)
                    {
                    }
                }
                while (!shutdown_.load())
                {
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                }
                try
                {
                    project_.cancelAll();
                    registration_.cancel();
                }
                catch (...)
                {
                }
                server_.stop();
            });
            const bool success = server_.listen_after_bind();
            shutdown_.store(true);
            monitor.join();
            std::error_code error;
            std::filesystem::remove(root / "session.json", error);
            return success ? 0 : 1;
        }

    private:
        static void reply(httplib::Response &_response,
                          const nlohmann::json &_body,
                          int _status = 200)
        {
            _response.status = _status;
            _response.set_content(_body.dump(), "application/json; charset=utf-8");
        }

        static void serveFile(httplib::Response &_response,
                              const std::filesystem::path &_path,
                              const std::string &_type)
        {
            auto stream = std::make_shared<std::ifstream>(_path, std::ios::binary);
            if (!*stream)
            {
                reply(_response, {{"error", "File not found"}}, 404);
                return;
            }
            const auto size = std::filesystem::file_size(_path);
            _response.set_content_provider(size, _type, [stream](std::size_t _offset, std::size_t _length, httplib::DataSink &_sink)
            {
                stream->clear();
                stream->seekg(static_cast<std::streamoff>(_offset));
                std::vector<char> buffer(std::min<std::size_t>(_length, 1024 * 1024));
                while (_length)
                {
                    const auto take = std::min(_length, buffer.size());
                    stream->read(buffer.data(), static_cast<std::streamsize>(take));
                    if (!*stream || !_sink.write(buffer.data(), take))
                    {
                        return false;
                    }
                    _length -= take;
                }
                return true;
            });
        }

        void routes()
        {
            server_.Get("/", [this](const httplib::Request &, httplib::Response &_response)
            {
                serveFile(_response, web_root_ / "index.html", "text/html; charset=utf-8");
            });
            server_.Get("/(app\\.js|viewer\\.js|registration\\.js|placement\\.js)", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                serveFile(_response, web_root_ / _request.matches[1].str(), "text/javascript; charset=utf-8");
            });
            server_.Get("/(app\\.css|viewer\\.css|placement\\.css)", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                serveFile(_response, web_root_ / _request.matches[1].str(), "text/css; charset=utf-8");
            });
            server_.Get("/viewer.html", [this](const httplib::Request &, httplib::Response &_response)
            {
                serveFile(_response, web_root_ / "viewer.html", "text/html; charset=utf-8");
            });
            server_.Get("/placement.html", [this](const httplib::Request &, httplib::Response &_response)
            {
                serveFile(_response, web_root_ / "placement.html", "text/html; charset=utf-8");
            });
            server_.Get("/api/placement-sample/([0-9a-f]{32})", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                const auto cloud = project_.pointCloud(_request.matches[1]);
                _response.set_content(placementSample(std::filesystem::u8path(cloud.at("path").get<std::string>()),
                                                      std::stoull(_request.get_param_value("start")),
                                                      std::stoull(_request.get_param_value("stride"))),
                                      "application/octet-stream");
            });
            server_.Get("/api/registration/placement/([0-9a-f]{32})", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                reply(_response, registration_.placementData(_request.matches[1]));
            });
            server_.Get("/api/registration/placement-sample/([0-9a-f]{32})/([0-9]+)", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                _response.set_content(placementSample(registration_.placementScan(_request.matches[1], std::stoull(_request.matches[2])),
                                                      std::stoull(_request.get_param_value("start")),
                                                      std::stoull(_request.get_param_value("stride"))),
                                      "application/octet-stream");
            });
            server_.Get("/api/project", [this](const httplib::Request &, httplib::Response &_response)
            {
                reply(_response, project_.snapshot());
            });
            server_.Get("/api/registration", [this](const httplib::Request &, httplib::Response &_response)
            {
                reply(_response, registration_.snapshot());
            });
            server_.Post("/api/registration/runtime", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                registration_.setup(nlohmann::json::parse(_request.body));
                reply(_response, {{"ok", true}});
            });
            server_.Post("/api/registration/start", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                registration_.start(nlohmann::json::parse(_request.body));
                reply(_response, {{"ok", true}});
            });
            server_.Post("/api/registration/draft", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                registration_.saveDraft(nlohmann::json::parse(_request.body));
                reply(_response, {{"ok", true}});
            });
            server_.Post("/api/registration/placements/save", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                reply(_response, registration_.placements(nlohmann::json::parse(_request.body), true));
            });
            server_.Post("/api/registration/placements/load", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                reply(_response, registration_.placements(nlohmann::json::parse(_request.body), false));
            });
            server_.Post("/api/registration/cancel", [this](const httplib::Request &, httplib::Response &_response)
            {
                registration_.cancel();
                reply(_response, {{"ok", true}});
            });
            server_.Post("/api/registration/generate/([0-9a-f]{32})", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                registration_.generateProduct(_request.matches[1], nlohmann::json::parse(_request.body).at("product"));
                reply(_response, registration_.snapshot());
            });
            server_.Post("/api/registration/import", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                const auto id = registration_.importResult(nlohmann::json::parse(_request.body).at("path"));
                reply(_response, {{"id", id}});
            });
            server_.Get("/files/result/([0-9a-f]{64})/([0-9a-f]{32})/(.+)", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                if (_request.matches[1] != token_)
                {
                    throw std::runtime_error("Reopen this result from BLK Studio");
                }
                const auto path = registration_.resultFile(_request.matches[2], _request.matches[3]);
                const auto extension = path.extension().string();
                std::string type = "application/octet-stream";
                if (extension == ".html")
                    type = "text/html; charset=utf-8";
                else if (extension == ".js")
                    type = "text/javascript; charset=utf-8";
                else if (extension == ".css")
                    type = "text/css; charset=utf-8";
                else if (extension == ".png")
                    type = "image/png";
                else if (extension == ".json")
                    type = "application/json; charset=utf-8";
                else if (extension == ".csv")
                    type = "text/csv; charset=utf-8";
                serveFile(_response, path, type);
            });
            server_.Post("/api/registration/open/([0-9a-f]{32})", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                openLocalTarget(registration_.outputDirectory(_request.matches[1]).u8string());
                reply(_response, {{"ok", true}});
            });
            server_.Post("/api/registration/chunks/([0-9a-f]{32})", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                const auto page = registration_.outputDirectory(_request.matches[1]) / "chunks_review.html";
                if (!std::filesystem::is_regular_file(page))
                {
                    throw std::runtime_error("Build the chunk review first");
                }
                openLocalTarget(page.u8string());
                reply(_response, {{"ok", true}});
            });
            server_.Post("/api/registration/review/([0-9a-f]{32})", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                const auto page = registration_.reviewPath(_request.matches[1]);
                if (!std::filesystem::is_regular_file(page))
                {
                    throw std::runtime_error("Review images are not available for this map");
                }
                if (page.filename() != "result_review_and_manage.html")
                {
                    openLocalTarget(page.u8string());
                    reply(_response, {{"ok", true}});
                    return;
                }
                const auto url = "http://127.0.0.1:" + std::to_string(port_) + "/files/result/" + token_ + "/" + _request.matches[1].str() + "/result_review_and_manage.html";
                openLocalTarget(url);
                reply(_response, {{"ok", true}});
            });
            server_.Get("/api/browse", [](const httplib::Request &_request, httplib::Response &_response)
            {
                const auto type = _request.get_param_value("type");
                reply(_response, browseFiles(_request.get_param_value("path"), type == "ply" ? ".ply" : type == "json" ? ".json"
                                                                                                                       : ".blk"));
            });
            server_.Post("/api/add-clouds", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                reply(_response, project_.addPointClouds(nlohmann::json::parse(_request.body).at("paths").get<std::vector<std::string>>()));
            });
            server_.Post("/api/refresh-clouds", [this](const httplib::Request &, httplib::Response &_response)
            {
                project_.refreshClouds();
                reply(_response, {{"ok", true}});
            });
            server_.Get("/api/cloud/([0-9a-f]{32})", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                reply(_response, project_.pointCloud(_request.matches[1]));
            });
            server_.Get("/files/cloud/([0-9a-f]{32})\\.ply", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                const auto cloud = project_.pointCloud(_request.matches[1]);
                serveFile(_response, std::filesystem::u8path(cloud.at("path").get<std::string>()), "application/octet-stream");
            });
            server_.Post("/api/add", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                reply(_response, project_.addPaths(nlohmann::json::parse(_request.body).at("paths").get<std::vector<std::string>>()));
            });
            server_.Post("/api/output", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                project_.setOutput(nlohmann::json::parse(_request.body).at("path"));
                reply(_response, {{"ok", true}});
            });
            server_.Post("/api/create-folder", [](const httplib::Request &_request, httplib::Response &_response)
            {
                const auto body = nlohmann::json::parse(_request.body);
                const std::string name = body.at("name");
                if (name.empty() || name == "." || name == ".." || name.find_first_of("/\\") != std::string::npos || name.find('\0') != std::string::npos)
                {
                    throw std::runtime_error("Enter a single folder name");
                }
                const auto path = std::filesystem::canonical(std::filesystem::u8path(body.at("parent").get<std::string>())) / std::filesystem::u8path(name);
                if (!std::filesystem::create_directory(path))
                {
                    throw std::runtime_error("Folder already exists");
                }
                reply(_response, {{"path", path.u8string()}});
            });
            server_.Post("/api/start", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                const auto body = nlohmann::json::parse(_request.body);
                project_.queue(body.value("ids", nlohmann::json()));
                reply(_response, {{"ok", true}});
            });
            server_.Post("/api/cancel", [this](const httplib::Request &, httplib::Response &_response)
            {
                project_.cancelAll();
                reply(_response, {{"ok", true}});
            });
            server_.Post("/api/retry/([0-9a-f]{32})", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                project_.retry(_request.matches[1]);
                reply(_response, {{"ok", true}});
            });
            server_.Post("/api/remove/([0-9a-f]{32})", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                project_.remove(_request.matches[1]);
                reply(_response, {{"ok", true}});
            });
            server_.Post("/api/name", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                project_.rename(nlohmann::json::parse(_request.body).at("name"));
                reply(_response, {{"ok", true}});
            });
            server_.Post("/api/open-folder", [this](const httplib::Request &, httplib::Response &_response)
            {
                const auto path = project_.snapshot().value("output_directory", "");
                if (path.empty() || !std::filesystem::is_directory(std::filesystem::u8path(path)))
                {
                    throw std::runtime_error("Output folder does not exist yet");
                }
                openLocalTarget(path);
                reply(_response, {{"ok", true}});
            });
            server_.Post("/api/shutdown", [this](const httplib::Request &, httplib::Response &_response)
            {
                shutdown_.store(true);
                reply(_response, {{"ok", true}});
            });
            server_.Get("/api/export-project", [this](const httplib::Request &, httplib::Response &_response)
            {
                _response.set_header("Content-Disposition", "attachment; filename=project.json");
                reply(_response, project_.snapshot());
            });
            server_.Get("/api/scan/([0-9a-f]{32})", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                reply(_response, project_.scan(_request.matches[1]));
            });
            server_.Get("/files/([0-9a-f]{32})\\.ply", [this](const httplib::Request &_request, httplib::Response &_response)
            {
                serveFile(_response, project_.outputPath(_request.matches[1]), "application/octet-stream");
            });
        }

        Project project_;
        std::filesystem::path web_root_;
        Registration registration_;
        std::string token_;
        int port_ = 0;
        httplib::Server server_;
        std::atomic<bool> shutdown_{false};
    };
} //namespace blk_core
