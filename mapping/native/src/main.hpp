#pragma once

#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

#include <Eigen/Core>
#include <json.hpp>
#include <kiss_matcher/KISSMatcher.hpp>
#include <nano_gicp/nano_gicp.hpp>
#include <nano_gicp/point_type_nano_gicp.hpp>
#include <omp.h>
#include <tbb/global_control.h>

namespace blk_mapping
{
    class Application
    {
    public:
        int run(const std::vector<std::string> &_arguments)
        {
            try
            {
                if (_arguments.size() == 1 && _arguments[0] == "--version")
                {
                    std::cout << versions().dump() << '\n';
                    return 0;
                }
                if (_arguments.size() == 1 && _arguments[0] == "--serve")
                {
                    // Line-delimited requests from the trajectory KISS adapter.
                    std::string line;
                    while (std::getline(std::cin, line))
                    {
                        nlohmann::json response;
                        try
                        {
                            const auto request = nlohmann::json::parse(line);
                            response = execute(request);
                        }
                        catch (const std::exception &error)
                        {
                            response = {{"error", error.what()}};
                        }
                        std::cout << "BLK_RESULT " << response.dump() << std::endl;
                    }
                    return 0;
                }
                if (_arguments.size() != 2)
                {
                    throw std::runtime_error("Usage: blk_registration REQUEST.json RESULT.json");
                }
                const auto request_path = std::filesystem::u8path(_arguments[0]);
                std::ifstream stream(request_path);
                const auto request = nlohmann::json::parse(stream);
                const auto result = execute(request);
                std::ofstream output(std::filesystem::u8path(_arguments[1]));
                output << result.dump(2) << '\n';
                if (!output)
                {
                    throw std::runtime_error("Cannot save native registration result");
                }
                return 0;
            }
            catch (const std::exception &error)
            {
                std::cerr << error.what() << '\n';
                return 1;
            }
        }

    private:
        nlohmann::json execute(const nlohmann::json &request)
        {
            const auto threads = request.value("threads", 8);
            if (threads < 1 || threads > 1024) throw std::runtime_error("Invalid thread count");
            // KISS uses oneTBB as well as OpenMP; both honor the same CPU budget.
            const tbb::global_control parallelism(tbb::global_control::max_allowed_parallelism, threads);
            omp_set_num_threads(threads);
            const auto source = readPoints(request.at("source"));

            const auto method = request.at("method").get<std::string>();
            nlohmann::json result;
            if (method == "kiss")
            {
                const auto target = readPoints(request.at("target"));
                kiss_matcher::KISSMatcherConfig config(request.value("resolution", 0.30F), false);
                config.robin_mode_ = "max_core";
                kiss_matcher::KISSMatcher matcher(config);
                const auto solution = matcher.estimate(source, target);
                Eigen::Matrix4d transform = Eigen::Matrix4d::Identity();
                transform.block<3, 3>(0, 0) = solution.rotation;
                transform.block<3, 1>(0, 3) = solution.translation;
                result = {{"transform_target_from_source", matrixJson(transform)},
                          {"valid", solution.valid && transform.allFinite()},
                          {"inliers", matcher.getNumFinalInliers()},
                          {"method", "KISS-Matcher"}};
            }
            else if (method == "nano")
            {
                nano_gicp::NanoGICP<PointType, PointType> matcher;
                matcher.setNumThreads(threads);
                matcher.setCorrespondenceRandomness(20);
                matcher.setInputTarget(pointCloud(readPoints(request.at("target"))));
                if (!matcher.calculateTargetCovariances())
                    throw std::runtime_error("Nano-GICP target covariance estimation failed");
                matcher.setMaximumIterations(request.value("iterations", 60));
                matcher.setMaxCorrespondenceDistance(request.value("max_distance", .16));
                matcher.setTransformationEpsilon(1e-6);
                matcher.setRotationEpsilon(1e-6);
                matcher.setInputSource(pointCloud(source));
                if (!matcher.calculateSourceCovariances())
                    throw std::runtime_error("Nano-GICP source covariance estimation failed");
                Eigen::Matrix4f initial = Eigen::Matrix4f::Identity();
                if (request.contains("initial"))
                {
                    for (int row = 0; row < 4; ++row)
                    {
                        for (int column = 0; column < 4; ++column)
                        {
                            initial(row, column) = request["initial"].at(row).at(column).get<float>();
                        }
                    }
                }
                pcl::PointCloud<PointType> aligned;
                matcher.align(aligned, initial);
                const Eigen::Matrix4d transform = matcher.getFinalTransformation().cast<double>();
                result = {{"transform_target_from_source", matrixJson(transform)},
                          {"valid", matcher.hasConverged() && transform.allFinite()},
                          {"method", "Nano-GICP"}};
            }
            else
            {
                throw std::runtime_error("Unknown registration method");
            }
            result["versions"] = versions();
            return result;
        }

        static nlohmann::json versions()
        {
            return {{"protocol", 1}, {"trajectory_session", 1},
                    {"kiss_matcher", "1.0.2"},
                    {"kiss_commit", "e3440b63340af7414ebd0fdde04e40f02ff4e7d2"},
                    {"nano_gicp_commit", "b21e79edcceb7c6e86ec0dfec90f451b796b683a"},
                    {"pcl", "1.15.1"}};
        }

        static std::vector<Eigen::Vector3f> readPoints(const std::string &_filename)
        {
            const auto path = std::filesystem::u8path(_filename);
            const auto size = std::filesystem::file_size(path);
            if (size % 12 != 0 || size < 20 * 12 || size > 12ULL * 100000000)
            {
                throw std::runtime_error("Expected 20 to 100 million little-endian float32 XYZ records");
            }
            std::ifstream stream(path, std::ios::binary);
            std::vector<Eigen::Vector3f> points;
            points.reserve(size / 12);
            for (std::uint64_t index = 0; index < size / 12; ++index)
            {
                float values[3];
                stream.read(reinterpret_cast<char *>(values), sizeof(values));
                if (!stream || !std::isfinite(values[0]) || !std::isfinite(values[1]) || !std::isfinite(values[2]))
                {
                    throw std::runtime_error("Invalid point record");
                }
                points.emplace_back(values[0], values[1], values[2]);
            }
            return points;
        }

        static pcl::PointCloud<PointType>::Ptr pointCloud(const std::vector<Eigen::Vector3f> &_points)
        {
            auto cloud = pcl::make_shared<pcl::PointCloud<PointType>>();
            cloud->reserve(_points.size());
            for (const auto &point : _points)
            {
                PointType value;
                value.x = point.x();
                value.y = point.y();
                value.z = point.z();
                value.intensity = 0;
                cloud->push_back(value);
            }
            return cloud;
        }

        static nlohmann::json matrixJson(const Eigen::Matrix4d &_matrix)
        {
            nlohmann::json rows = nlohmann::json::array();
            for (int row = 0; row < 4; ++row)
            {
                rows.push_back({_matrix(row, 0), _matrix(row, 1), _matrix(row, 2), _matrix(row, 3)});
            }
            return rows;
        }
    };
} //namespace blk_mapping
