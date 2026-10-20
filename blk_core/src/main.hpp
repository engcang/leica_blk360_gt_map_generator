#pragma once

#include <atomic>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include <httplib.h>
#include <json.hpp>

#include "web_server.hpp"

namespace blk_core
{
    class WebApplication
    {
    public:
        int run(const std::vector<std::string> &_arguments) const
        {
            try
            {
                const auto directory = executablePath().parent_path();
                auto project_root = directory / "projects" / (rgbEdition ? "rgb" : "default");
                bool open_browser = true;
                for (std::size_t i = 0; i < _arguments.size(); ++i)
                {
                    if (_arguments[i] == "--no-browser")
                    {
                        open_browser = false;
                    }
                    else if (_arguments[i] == "--project" && i + 1 < _arguments.size())
                    {
                        project_root = std::filesystem::absolute(std::filesystem::u8path(_arguments[++i]));
                    }
                    else
                    {
                        throw std::runtime_error("Options: --project DIRECTORY --no-browser");
                    }
                }
                std::filesystem::create_directories(project_root);
                InstanceLock lock(project_root / "project.lock");
                if (!lock.acquired())
                {
                    const auto session = nlohmann::json::parse(readText(project_root / "session.json"));
                    const int port = session.at("port");
                    const std::string token = session.at("token");
                    httplib::Client client("127.0.0.1", port);
                    client.set_connection_timeout(2);
                    const auto response = client.Get("/api/project", {{"X-Session-Token", token}});
                    if (!response || response->status != 200)
                    {
                        throw std::runtime_error("This project is already open. Wait a moment and reopen the application.");
                    }
                    if (open_browser)
                    {
                        openLocalTarget("http://127.0.0.1:" + std::to_string(port) + "/#" + token);
                    }
                    return 0;
                }
                if (!std::filesystem::exists(directory / (rgbEdition ? "web_rgb" : "web") / "index.html"))
                {
                    throw std::runtime_error("Keep the web folder beside the application. Extract the complete ZIP before opening it.");
                }
                return WebServer(project_root, directory / (rgbEdition ? "web_rgb" : "web")).run(open_browser);
            }
            catch (const std::exception &error)
            {
#ifdef _WIN32
                const auto message = std::filesystem::u8path(error.what()).wstring();
                MessageBoxW(nullptr, message.c_str(), L"BLK Studio", MB_OK | MB_ICONERROR);
#else
                std::cerr << error.what() << std::endl;
#endif
                return 1;
            }
        }
    };

    class MainApplication
    {
    public:
        int run(int _argc,
                char **_argv) const
        {
            try
            {
                if (_argc != 3)
                {
                    std::cout << "blk_convert INPUT.blk OUTPUT.ply\n";
                    return 2;
                }
                std::atomic<bool> cancel(false);
                const auto result = Converter().convert(std::filesystem::u8path(_argv[1]), std::filesystem::u8path(_argv[2]), cancel, [](const Progress &_progress)
                {
                    std::cerr << static_cast<int>(_progress.fraction_ * 100) << "% " << _progress.stage_ << std::endl;
                });
                std::cout << result.dump(2) << '\n';
                return 0;
            }
            catch (const std::exception &error)
            {
                std::cerr << error.what() << std::endl;
                return 1;
            }
        }
    };
} //namespace blk_core
