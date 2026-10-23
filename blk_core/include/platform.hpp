#pragma once

#include <cstdint>
#include <cerrno>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#include <windows.h>
#include <shellapi.h>
#else
#include <fcntl.h>
#include <sys/file.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif
#endif

namespace blk_core
{
    inline void publishFile(const std::filesystem::path &_source,
                            const std::filesystem::path &_target)
    {
#ifdef _WIN32
        if (!MoveFileExW(_source.c_str(), _target.c_str(), 0))
        {
            throw std::runtime_error("Cannot publish PLY; the output may already exist or the folder may not be writable");
        }
#else
        if (link(_source.c_str(), _target.c_str()) == 0)
        {
            std::filesystem::remove(_source);
            return;
        }
        //Codex: Exclusive creation also supports filesystems without hard links, without replacing existing data.
        const int descriptor = open(_target.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0666);
        if (descriptor < 0)
        {
            throw std::runtime_error("Cannot create PLY; output exists or folder is not writable");
        }
        try
        {
            std::ifstream input(_source, std::ios::binary);
            std::vector<char> buffer(1024 * 1024);
            while (input)
            {
                input.read(buffer.data(), buffer.size());
                std::size_t offset = 0;
                const auto count = static_cast<std::size_t>(input.gcount());
                while (offset < count)
                {
                    const auto written = write(descriptor, buffer.data() + offset, count - offset);
                    if (written < 0 && errno == EINTR)
                    {
                        continue;
                    }
                    if (written <= 0)
                    {
                        throw std::runtime_error("Cannot write PLY output");
                    }
                    offset += static_cast<std::size_t>(written);
                }
            }
            if (!input.eof())
            {
                throw std::runtime_error("Cannot read completed PLY");
            }
        }
        catch (...)
        {
            close(descriptor);
            std::filesystem::remove(_target);
            throw;
        }
        if (close(descriptor) != 0)
        {
            std::filesystem::remove(_target);
            throw std::runtime_error("Cannot close PLY output");
        }
        std::filesystem::remove(_source);
#endif
    }

    inline void publishDirectory(const std::filesystem::path &_source,
                                 const std::filesystem::path &_target)
    {
        if (!std::filesystem::create_directory(_target))
        {
            throw std::runtime_error("Output already exists: " + _target.u8string());
        }
        for (const auto &entry : std::filesystem::directory_iterator(_source))
        {
            if (entry.is_regular_file())
            {
                publishFile(entry.path(), _target / entry.path().filename());
            }
        }
        std::filesystem::remove(_source);
    }

    inline void replaceFile(const std::filesystem::path &_source,
                            const std::filesystem::path &_target)
    {
#ifdef _WIN32
        if (!MoveFileExW(_source.c_str(), _target.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
        {
            throw std::runtime_error("Cannot save project manifest");
        }
#else
        std::filesystem::rename(_source, _target);
#endif
    }

    class InstanceLock
    {
    public:
        explicit InstanceLock(const std::filesystem::path &_path)
        {
#ifdef _WIN32
            handle_ = CreateFileW(_path.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
#else
            descriptor_ = open(_path.c_str(), O_CREAT | O_RDWR, 0600);
            locked_ = descriptor_ >= 0 && flock(descriptor_, LOCK_EX | LOCK_NB) == 0;
#endif
        }
        InstanceLock(const InstanceLock &) = delete;
        InstanceLock &operator=(const InstanceLock &) = delete;
        ~InstanceLock()
        {
#ifdef _WIN32
            if (acquired())
            {
                CloseHandle(handle_);
            }
#else
            if (descriptor_ >= 0)
            {
                close(descriptor_);
            }
#endif
        }
        bool acquired() const
        {
#ifdef _WIN32
            return handle_ != INVALID_HANDLE_VALUE;
#else
            return locked_;
#endif
        }

    private:
#ifdef _WIN32
        HANDLE handle_ = INVALID_HANDLE_VALUE;
#else
        int descriptor_ = -1;
        bool locked_ = false;
#endif
    };

    inline std::filesystem::path executablePath()
    {
#ifdef _WIN32
        std::vector<wchar_t> buffer(32768);
        const auto size = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
        if (size == 0 || size == buffer.size())
        {
            throw std::runtime_error("Cannot locate application");
        }
        return std::filesystem::path(std::wstring(buffer.data(), size));
#elif defined(__APPLE__)
        std::uint32_t size = 0;
        _NSGetExecutablePath(nullptr, &size);
        std::vector<char> buffer(size);
        if (_NSGetExecutablePath(buffer.data(), &size) != 0)
        {
            throw std::runtime_error("Cannot locate application");
        }
        return std::filesystem::weakly_canonical(buffer.data());
#else
        std::vector<char> buffer(32768);
        const auto size = readlink("/proc/self/exe", buffer.data(), buffer.size());
        if (size <= 0 || static_cast<std::size_t>(size) == buffer.size())
        {
            throw std::runtime_error("Cannot locate application");
        }
        return std::filesystem::path(std::string(buffer.data(), size));
#endif
    }

    inline void openLocalTarget(const std::string &_target)
    {
#ifdef _WIN32
        const auto wide = std::filesystem::u8path(_target).wstring();
        const auto result = reinterpret_cast<std::intptr_t>(ShellExecuteW(nullptr, L"open", wide.c_str(), nullptr, nullptr, SW_SHOWNORMAL));
        if (result <= 32)
        {
            throw std::runtime_error("Could not open the browser or folder");
        }
#else
        const auto child = fork();
        if (child == 0)
        {
#ifdef __APPLE__
            execlp("open", "open", _target.c_str(), static_cast<char *>(nullptr));
#else
            execlp("xdg-open", "xdg-open", _target.c_str(), static_cast<char *>(nullptr));
#endif
            _exit(127);
        }
        if (child < 0)
        {
            throw std::runtime_error("Could not open the browser or folder");
        }
        std::thread([child]
        {
            int status = 0;
            waitpid(child, &status, 0);
        }).detach();
#endif
    }
} //namespace blk_core
