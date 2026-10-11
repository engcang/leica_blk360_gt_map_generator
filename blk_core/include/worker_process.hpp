#pragma once

#include <atomic>
#include <cerrno>
#include <chrono>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "platform.hpp"

#ifndef _WIN32
#include <csignal>
#include <spawn.h>
extern char **environ;
#endif

namespace blk_core
{
#ifdef _WIN32
    inline std::wstring quoteProcessArgument(const std::string &_argument)
    {
        const auto value = std::filesystem::u8path(_argument).wstring();
        std::wstring result = L"\"";
        std::size_t slashes = 0;
        for (const auto character : value)
        {
            if (character == L'\\')
            {
                ++slashes;
                continue;
            }
            result.append(character == L'"' ? 2 * slashes + 1 : slashes, L'\\');
            result += character;
            slashes = 0;
        }
        result.append(2 * slashes, L'\\');
        return result + L'"';
    }

    class ProcessHandle
    {
    public:
        explicit ProcessHandle(HANDLE _handle):
            handle_(_handle)
        {
        }
        ~ProcessHandle()
        {
            if (handle_ && handle_ != INVALID_HANDLE_VALUE)
            {
                CloseHandle(handle_);
            }
        }
        ProcessHandle(const ProcessHandle &) = delete;
        ProcessHandle &operator=(const ProcessHandle &) = delete;
        HANDLE get() const
        {
            return handle_;
        }

    private:
        HANDLE handle_;
    };
#endif

    inline int runWorkerProcess(const std::vector<std::string> &_arguments,
                                const std::filesystem::path &_log,
                                const std::atomic<bool> &_cancel)
    {
        if (_arguments.empty())
        {
            throw std::runtime_error("Worker command is empty");
        }
        for (const auto &argument : _arguments)
        {
            if (argument.find('\0') != std::string::npos)
            {
                throw std::runtime_error("Worker arguments contain a null byte");
            }
        }
#ifdef _WIN32
        SECURITY_ATTRIBUTES attributes{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
        ProcessHandle log(CreateFileW(_log.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, &attributes, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr));
        ProcessHandle input(CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &attributes, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr));
        ProcessHandle group(CreateJobObjectW(nullptr, nullptr));
        if (log.get() == INVALID_HANDLE_VALUE || input.get() == INVALID_HANDLE_VALUE || !group.get())
        {
            throw std::runtime_error("Cannot create registration worker handles");
        }
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if (!SetInformationJobObject(group.get(), JobObjectExtendedLimitInformation, &limits, sizeof(limits)))
        {
            throw std::runtime_error("Cannot configure registration worker lifetime");
        }
        std::wstring command;
        for (const auto &argument : _arguments)
        {
            if (!command.empty())
            {
                command += L' ';
            }
            command += quoteProcessArgument(argument);
        }
        STARTUPINFOW startup{};
        startup.cb = sizeof(startup);
        startup.dwFlags = STARTF_USESTDHANDLES;
        startup.hStdOutput = log.get();
        startup.hStdError = log.get();
        startup.hStdInput = input.get();
        PROCESS_INFORMATION information{};
        if (!CreateProcessW(nullptr, command.data(), nullptr, nullptr, TRUE, CREATE_NO_WINDOW | CREATE_SUSPENDED, nullptr, nullptr, &startup, &information))
        {
            throw std::runtime_error("Cannot start Python. Install 64-bit Python 3.10-3.13 or select its executable in Registration setup");
        }
        ProcessHandle process(information.hProcess);
        ProcessHandle thread(information.hThread);
        if (!AssignProcessToJobObject(group.get(), process.get()))
        {
            TerminateProcess(process.get(), 1);
            WaitForSingleObject(process.get(), INFINITE);
            throw std::runtime_error("Cannot manage registration worker lifetime");
        }
        if (ResumeThread(thread.get()) == static_cast<DWORD>(-1))
        {
            throw std::runtime_error("Cannot resume registration worker");
        }
        while (WaitForSingleObject(process.get(), 100) == WAIT_TIMEOUT)
        {
            if (_cancel.load())
            {
                TerminateJobObject(group.get(), 1);
                WaitForSingleObject(process.get(), INFINITE);
                return -1;
            }
        }
        DWORD code = 1;
        GetExitCodeProcess(process.get(), &code);
        return static_cast<int>(code);
#else
        const int descriptor = open(_log.c_str(), O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
        if (descriptor < 0)
        {
            throw std::runtime_error("Cannot open registration worker log");
        }
        std::vector<char *> arguments;
        for (const auto &argument : _arguments)
        {
            arguments.push_back(const_cast<char *>(argument.c_str()));
        }
        arguments.push_back(nullptr);
        posix_spawn_file_actions_t actions;
        posix_spawnattr_t attributes;
        posix_spawn_file_actions_init(&actions);
        posix_spawnattr_init(&attributes);
        posix_spawn_file_actions_adddup2(&actions, descriptor, STDOUT_FILENO);
        posix_spawn_file_actions_adddup2(&actions, descriptor, STDERR_FILENO);
        posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0);
        posix_spawn_file_actions_addclose(&actions, descriptor);
        posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETPGROUP);
        posix_spawnattr_setpgroup(&attributes, 0);
        pid_t process = 0;
        const int error = posix_spawnp(&process, arguments.front(), &actions, &attributes, arguments.data(), environ);
        posix_spawn_file_actions_destroy(&actions);
        posix_spawnattr_destroy(&attributes);
        close(descriptor);
        if (error)
        {
            throw std::runtime_error("Cannot start Python. Install Python 3.10-3.13 or select its executable in Registration setup");
        }
        int status = 0;
        for (;;)
        {
            const auto result = waitpid(process, &status, WNOHANG);
            if (result == process)
            {
                return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
            }
            if (result < 0 && errno != EINTR)
            {
                kill(-process, SIGKILL);
                throw std::runtime_error("Cannot monitor registration worker");
            }
            if (_cancel.load())
            {
                kill(-process, SIGKILL);
                while (waitpid(process, &status, 0) < 0 && errno == EINTR)
                {
                }
                return -1;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
#endif
    }
} //namespace blk_core
