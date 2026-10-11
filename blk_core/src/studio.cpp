#include "main.hpp"

#ifdef _WIN32
#include <windows.h>

int WINAPI wWinMain(HINSTANCE _instance,
                    HINSTANCE _previous,
                    PWSTR _command,
                    int _show)
{
    static_cast<void>(_instance);
    static_cast<void>(_previous);
    static_cast<void>(_command);
    static_cast<void>(_show);
    int count = 0;
    auto **arguments = CommandLineToArgvW(GetCommandLineW(), &count);
    std::vector<std::string> values;
    for (int i = 1; i < count; ++i)
    {
        values.push_back(std::filesystem::path(arguments[i]).u8string());
    }
    LocalFree(arguments);
    return blk_core::WebApplication().run(values);
}
#else
int main(int _argc,
         char **_argv)
{
    return blk_core::WebApplication().run(std::vector<std::string>(_argv + 1, _argv + _argc));
}
#endif
