#include "main.hpp"

#ifdef _WIN32
int wmain(int _argc,
          wchar_t **_argv)
{
    std::vector<std::string> arguments;
    for (int i = 1; i < _argc; ++i)
    {
        arguments.push_back(std::filesystem::path(_argv[i]).u8string());
    }
    return blk_mapping::Application().run(arguments);
}
#else
int main(int _argc,
         char **_argv)
{
    return blk_mapping::Application().run(std::vector<std::string>(_argv + 1, _argv + _argc));
}
#endif
