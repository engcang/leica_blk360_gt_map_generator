// Exact tiled nearest-neighbor queries. Python owns immutable point/normal buffers.
#include <cstdint>
#include <nanoflann.hpp>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

#ifdef _WIN32
#define BLK_EXPORT extern "C" __declspec(dllexport)
#else
#define BLK_EXPORT extern "C" __attribute__((visibility("default")))
#endif

namespace {
using Id = std::int64_t;
using Cell = std::array<Id, 3>;
constexpr double infinity = std::numeric_limits<double>::infinity();
thread_local std::string error;
struct CellHash {
    std::size_t operator()(const Cell &c) const {
        std::size_t h = 0;
        for (auto x : c) h ^= std::hash<Id>{}(x) + 0x9e3779b9 + (h << 6) + (h >> 2);
        return h;
    }
};
struct Tile {
    const float *points;
    const double *normals = nullptr;
    Id count;
    using Metric = nanoflann::L2_Simple_Adaptor<double, Tile, double>;
    using Tree = nanoflann::KDTreeSingleIndexAdaptor<Metric, Tile, 3, Id>;
    Tree tree;
    Tile(const float *p, Id n) : points(p), count(n), tree(3, *this, {16}) { tree.buildIndex(); }
    std::size_t kdtree_get_point_count() const { return count; }
    double kdtree_get_pt(Id i, std::size_t d) const { return points[3 * i + d]; }
    template<class Box> bool kdtree_get_bbox(Box &) const { return false; }
};
struct Index {
    std::vector<Cell> cells;
    std::vector<Id> offsets;
    std::vector<std::unique_ptr<Tile>> tiles;
    std::unordered_map<Cell, Id, CellHash> lookup;
    double width;
    int workers;
    Index(const Id *coords, const Id *starts, Id count, double w, int threads)
        : offsets(starts, starts + count + 1), tiles(count), width(w), workers(threads) {
        for (Id i = 0; i < count; ++i) {
            cells.push_back({coords[3*i], coords[3*i+1], coords[3*i+2]});
            lookup.emplace(cells.back(), i);
        }
    }
};
// A fixed-size result set merges candidates from all visited tiles without allocations.
struct Neighbors {
    int k, count = 0;
    double cap2;
    Id offset = 0;
    double *distances;
    Id *ids;
    bool full() const { return count == k; }
    double worstDist() const {
        return count < k ? cap2 : std::min(cap2, std::nextafter(distances[k-1], infinity));
    }
    bool addPoint(double d, Id local) {
        if (!(d < cap2)) return true;
        const Id id = local + offset;
        int pos = std::min(count, k-1);
        if (count == k && (d > distances[pos] || (d == distances[pos] && id >= ids[pos]))) return true;
        while (pos > 0 && (d < distances[pos-1] || (d == distances[pos-1] && id < ids[pos-1]))) {
            distances[pos] = distances[pos-1]; ids[pos] = ids[pos-1]; --pos;
        }
        distances[pos] = d; ids[pos] = id;
        count = std::min(k, count+1);
        return true;
    }
};

template<class F> int guarded(F fn) {
    try { fn(); return 0; }
    catch (const std::exception &e) { error = e.what(); return -1; }
}
}

BLK_EXPORT const char *blk_correspondence_error() { return error.c_str(); }
BLK_EXPORT int blk_correspondence_abi() { return 1; }
BLK_EXPORT void *blk_index_create(const Id *coords, const Id *offsets, Id count, double width, int workers) {
    try { return new Index(coords, offsets, count, width, workers); }
    catch (const std::exception &e) { error = e.what(); return nullptr; }
}
BLK_EXPORT void blk_index_destroy(void *handle) { delete static_cast<Index *>(handle); }
BLK_EXPORT int blk_index_add(void *handle, Id tile, const float *points) {
    return guarded([&] {
        auto &index = *static_cast<Index *>(handle);
        index.tiles.at(tile) = std::make_unique<Tile>(points, index.offsets.at(tile+1)-index.offsets.at(tile));
    });
}
BLK_EXPORT void blk_index_remove(void *handle, Id tile) { static_cast<Index *>(handle)->tiles[tile].reset(); }
BLK_EXPORT std::uint64_t blk_index_bytes(void *handle, Id tile) {
    auto &data = static_cast<Index *>(handle)->tiles.at(tile);
    // Reserve normals even before they are loaded; include mapped input points.
    return data->tree.usedMemory(data->tree) + data->count*(3*sizeof(float)+3*sizeof(double));
}
BLK_EXPORT void blk_index_normals(void *handle, Id tile, const double *normals) {
    static_cast<Index *>(handle)->tiles[tile]->normals = normals;
}
BLK_EXPORT int blk_index_query(void *handle, const double *points, Id n, int k, double cap,
                               const std::uint8_t *selected, double *distances, Id *ids) {
    return guarded([&] {
        const auto &index = *static_cast<Index *>(handle);
        const double cap2 = cap*cap;
        // Spatially ordered submaps contain regions with very different query
        // costs. Work stealing changes only scheduling, never row results.
        #pragma omp parallel for num_threads(index.workers) schedule(dynamic, 256)
        for (Id row = 0; row < n; ++row) {
            double *d = distances + row*k;
            Id *out = ids + row*k;
            std::fill(d, d+k, infinity); std::fill(out, out+k, index.offsets.back());
            if (selected && !selected[row]) continue;
            const double *p = points+3*row;
            Cell owner{};
            bool representable = true;
            for (int a = 0; a < 3; ++a) {
                double x = std::floor(p[a]/index.width);
                if (std::abs(x) > 1e15) { representable = false; break; }
                owner[a] = static_cast<Id>(x);
            }
            if (!representable) continue;
            Neighbors result{k, 0, cap2, 0, d, out};
            auto search = [&](const Cell &cell) {
                auto found = index.lookup.find(cell);
                if (found == index.lookup.end()) return;
                const Id tile = found->second;
                const auto &tree = index.tiles[tile];
                if (!tree) return;  // Caller loads the conservative query plan first.
                double bound = 0, scale = 1+cap;
                for (int a = 0; a < 3; ++a) {
                    const double low = cell[a]*index.width;
                    const double delta = std::max({low-p[a], p[a]-(low+index.width), 0.});
                    bound += delta*delta; scale += std::abs(p[a]);
                }
                const double margin = 128*std::numeric_limits<double>::epsilon()*scale;
                if (std::sqrt(bound) > std::sqrt(result.worstDist())+margin) return;
                result.offset = index.offsets[tile];
                tree->tree.findNeighbors(result, p, nanoflann::SearchParams());
            };
            search(owner);
            // Tighten the tile range with the owner's actual kth distance.
            // Most queries then need no adjacent tile or hash lookup at all.
            const double margin = 128*std::numeric_limits<double>::epsilon()*(1+cap+std::abs(p[0])+std::abs(p[1])+std::abs(p[2]));
            const double radius = std::sqrt(result.worstDist())+margin;
            Cell lower{}, upper{};
            for (int a = 0; a < 3; ++a) {
                lower[a] = static_cast<Id>(std::floor((p[a]-radius)/index.width));
                upper[a] = static_cast<Id>(std::floor((p[a]+radius)/index.width));
            }
            for (Id x = lower[0]; x <= upper[0]; ++x)
                for (Id y = lower[1]; y <= upper[1]; ++y)
                    for (Id z = lower[2]; z <= upper[2]; ++z)
                        if (Cell{x,y,z} != owner) search({x,y,z});
            for (int j = 0; j < k; ++j) d[j] = std::sqrt(d[j]);
        }
    });
}
BLK_EXPORT int blk_index_gather(void *handle, const Id *ids, Id n, double *points, double *normals) {
    return guarded([&] {
        const auto &index = *static_cast<Index *>(handle);
        int missing = 0;
        #pragma omp parallel for num_threads(index.workers) schedule(static) reduction(|:missing)
        for (Id row = 0; row < n; ++row) {
            const Id id = ids[row];
            if (id == index.offsets.back()) continue;
            if (id < 0 || id >= index.offsets.back()) { missing = 1; continue; }
            const auto tile = std::upper_bound(index.offsets.begin()+1, index.offsets.end(), id)-index.offsets.begin()-1;
            const auto &data = index.tiles[tile];
            if (!data || (normals && !data->normals)) { missing = 1; continue; }
            const Id local = id-index.offsets[tile];
            for (int a = 0; a < 3; ++a) {
                points[3*row+a] = data->points[3*local+a];
                if (normals) normals[3*row+a] = data->normals[3*local+a];
            }
        }
        if (missing) throw std::runtime_error("Missing tile/normal buffer in native gather");
    });
}

BLK_EXPORT void blk_certify(void *handle, const double *points, const double *reference,
                            const double *candidates, const Id *candidate_ids, const double *second,
                            Id count, double cap, double *distances, Id *ids,
                            std::uint8_t *certified, std::uint8_t *missing) {
    const auto &index = *static_cast<Index *>(handle);
    #pragma omp parallel for num_threads(index.workers) schedule(static)
    for (Id row = 0; row < count; ++row) {
        double motion2 = 0, distance2 = 0, p2 = 0, r2 = 0;
        for (int a = 0; a < 3; ++a) {
            const Id i = 3*row+a;
            const double motion = points[i]-reference[i], delta = points[i]-candidates[i];
            motion2 += motion*motion; distance2 += delta*delta;
            p2 += points[i]*points[i]; r2 += reference[i]*reference[i];
        }
        const double movement = std::sqrt(motion2), distance = std::sqrt(distance2);
        const double margin = 128*std::numeric_limits<double>::epsilon()*(1+cap+second[row]+std::sqrt(p2)+std::sqrt(r2));
        const bool present = candidate_ids[row] < index.offsets.back();
        certified[row] = present && distance+movement < second[row]-margin;
        missing[row] = !present && second[row]-movement > cap+margin;
        const bool keep = certified[row] && distance < cap;
        distances[row] = keep ? distance : infinity;
        ids[row] = keep ? candidate_ids[row] : index.offsets.back();
    }
}
