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
    bool seeded = false;
    bool full() const { return count == k; }
    double worstDist() const {
        return count < k ? cap2 : std::min(cap2, std::nextafter(distances[k-1], infinity));
    }
    bool addPoint(double d, Id local) {
        if (!(d < cap2)) return true;
        const Id id = local + offset;
        if (seeded) for (int j = 0; j < count; ++j) if (ids[j] == id) return true;
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

void search_neighbors(const Index &index, const double *p, int k, double cap, double *d, Id *out,
                      const Id *seed_ids = nullptr, const double *seed_distances2 = nullptr) {
    std::fill(d, d+k, infinity); std::fill(out, out+k, index.offsets.back());
    Cell owner{};
    for (int a = 0; a < 3; ++a) {
        const double x = std::floor(p[a]/index.width);
        if (std::abs(x) > 1e15) return;
        owner[a] = static_cast<Id>(x);
    }
    Neighbors result{k, 0, cap*cap, 0, d, out};
    if (seed_ids) {
        result.seeded = true;
        for (int i = 0; i < 2; ++i)
            if (seed_ids[i] < index.offsets.back()) result.addPoint(seed_distances2[i],seed_ids[i]);
    }
    auto search = [&](const Cell &cell) {
        auto found = index.lookup.find(cell);
        if (found == index.lookup.end()) return;
        const Id tile = found->second;
        const auto &tree = index.tiles[tile];
        if (!tree) return;  // The caller loads the conservative query plan first.
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

bool gather_row(const Index &index, Id id, double *xyz, double *normal) {
    if (id < 0 || id >= index.offsets.back()) return false;
    const auto tile = std::upper_bound(index.offsets.begin()+1, index.offsets.end(), id)-index.offsets.begin()-1;
    const auto &data = index.tiles[tile];
    if (!data || (normal && !data->normals)) return false;
    const Id local = id-index.offsets[tile];
    for (int a = 0; a < 3; ++a) {
        xyz[a] = data->points[3*local+a];
        if (normal) normal[a] = data->normals[3*local+a];
    }
    return true;
}

struct MatchState {
    Index &index;
    Id count;
    bool initialized = false;
    std::vector<double> reference, second, second_xyz;
    std::vector<Id> candidate_ids, second_ids;
    MatchState(Index &i, Id n) : index(i), count(n), reference(3*n), second(n),
        second_xyz(3*n), candidate_ids(n, i.offsets.back()), second_ids(n,i.offsets.back()) {}
};

struct VoxelRow { std::uint64_t key; Id row; };
template<class Scalar> Id voxel_rows(const Scalar *points, Id count, Scalar size, int workers, Id *chosen) {
    constexpr auto invalid_key = std::numeric_limits<std::uint64_t>::max();
    std::vector<VoxelRow> rows(count), scratch(count);
    int outside_grid = 0;
    #pragma omp parallel for num_threads(workers) schedule(static) reduction(|:outside_grid)
    for (Id i = 0; i < count; ++i) {
        rows[i] = {invalid_key,i};
        const Scalar *p = points+3*i;
        if (!std::isfinite(p[0]) || !std::isfinite(p[1]) || !std::isfinite(p[2])) continue;
        std::uint64_t key = 0;
        for (int a = 0; a < 3; ++a) {
            // Preserve NumPy's input precision, including float32 boundaries.
            const Scalar cell = std::floor(Scalar(p[a]/size));
            if (!(cell >= -(1<<20) && cell < (1<<20))) { outside_grid = 1; break; }
            key = (key<<21) | std::uint64_t(Id(cell)+(1<<20));
        }
        rows[i].key = key;
    }
    if (outside_grid) return -1;  // General integer-grid ordering stays on the reference path.
    const Id chunks = std::min<Id>(count,std::max(1,workers*4));
    if (!chunks) return 0;
    std::vector<Id> histogram(chunks*256), offsets(chunks*256);
    for (unsigned shift = 0; shift < 64; shift += 8) {
        std::fill(histogram.begin(),histogram.end(),0);
        #pragma omp parallel for num_threads(workers) schedule(static)
        for (Id chunk = 0; chunk < chunks; ++chunk)
            for (Id i = count*chunk/chunks; i < count*(chunk+1)/chunks; ++i)
                ++histogram[chunk*256+((rows[i].key>>shift)&255)];
        Id offset = 0;
        for (int bucket = 0; bucket < 256; ++bucket)
            for (Id chunk = 0; chunk < chunks; ++chunk) {
                const Id at = chunk*256+bucket;
                offsets[at] = offset; offset += histogram[at];
            }
        #pragma omp parallel for num_threads(workers) schedule(static)
        for (Id chunk = 0; chunk < chunks; ++chunk)
            for (Id i = count*chunk/chunks; i < count*(chunk+1)/chunks; ++i)
                scratch[offsets[chunk*256+((rows[i].key>>shift)&255)]++] = rows[i];
        rows.swap(scratch);
    }
    Id used = 0;
    for (Id i = 0; i < count && rows[i].key != invalid_key; ++i)
        if (!i || rows[i].key != rows[i-1].key) chosen[used++] = rows[i].row;
    return used;
}

template<class F> int guarded(F fn) {
    try { fn(); return 0; }
    catch (const std::exception &e) { error = e.what(); return -1; }
}
}

BLK_EXPORT const char *blk_correspondence_error() { return error.c_str(); }
BLK_EXPORT int blk_correspondence_abi() { return 1; }
BLK_EXPORT int blk_voxel_rows(const void *points, Id count, int float32, double size,
                              int workers, Id *chosen, Id *used) {
    return guarded([&] {
        if (float32) *used = voxel_rows(static_cast<const float *>(points),count,float(size),workers,chosen);
        else *used = voxel_rows(static_cast<const double *>(points),count,size,workers,chosen);
    });
}
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
BLK_EXPORT int blk_index_add_batch(void *handle, const Id *tiles, const float *const *points, Id count) {
    return guarded([&] {
        auto &index = *static_cast<Index *>(handle);
        std::vector<std::string> failures(count);
        #pragma omp parallel for num_threads(index.workers) schedule(dynamic,1)
        for (Id i = 0; i < count; ++i) {
            try {
                const Id tile = tiles[i];
                index.tiles.at(tile) = std::make_unique<Tile>(points[i],index.offsets.at(tile+1)-index.offsets.at(tile));
            } catch (const std::exception &e) { failures[i] = e.what(); }
        }
        for (const auto &failure : failures) if (!failure.empty()) {
            // Python releases the whole uncommitted batch on failure.
            for (Id i = 0; i < count; ++i) index.tiles.at(tiles[i]).reset();
            throw std::runtime_error(failure);
        }
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
// The native query needs a set of resident tiles, not Python's per-tile lists
// of source rows. Build only that set; conservative boxes may load extra tiles
// but never remove a possible neighbor from the exact query below.
BLK_EXPORT int blk_index_plan(void *handle, const double *points, Id n, double radius,
                              std::uint8_t *needed) {
    return guarded([&] {
        const auto &index = *static_cast<Index *>(handle);
        std::fill(needed, needed+index.cells.size(), 0);
        #pragma omp parallel num_threads(index.workers)
        {
            std::vector<std::uint8_t> local(index.cells.size(), 0);
            #pragma omp for schedule(static)
            for (Id row = 0; row < n; ++row) {
                const double *p = points+3*row;
                const double extent = std::max({std::abs(p[0]), std::abs(p[1]), std::abs(p[2])});
                if (std::abs(std::floor(p[0]/index.width)) > 1e15 ||
                    std::abs(std::floor(p[1]/index.width)) > 1e15 ||
                    std::abs(std::floor(p[2]/index.width)) > 1e15) continue;
                const double margin = 128*std::numeric_limits<double>::epsilon()*(1+radius+extent);
                Cell lower{}, upper{};
                for (int a = 0; a < 3; ++a) {
                    lower[a] = static_cast<Id>(std::floor((p[a]-radius-margin)/index.width));
                    upper[a] = static_cast<Id>(std::floor((p[a]+radius+margin)/index.width));
                }
                for (Id x = lower[0]; x <= upper[0]; ++x)
                    for (Id y = lower[1]; y <= upper[1]; ++y)
                        for (Id z = lower[2]; z <= upper[2]; ++z) {
                            auto found = index.lookup.find({x,y,z});
                            if (found != index.lookup.end()) local[found->second] = 1;
                        }
            }
            #pragma omp critical(blk_plan_merge)
            {
                for (std::size_t tile = 0; tile < local.size(); ++tile) needed[tile] |= local[tile];
            }
        }
    });
}
BLK_EXPORT int blk_plan_reusable(void *handle, const double *points, const double *reference,
                                 Id n, double cap, double radius) {
    const auto &index = *static_cast<Index *>(handle);
    double motion2 = 0, extent = 0;
    #pragma omp parallel num_threads(index.workers)
    {
        double local_motion2 = 0, local_extent = 0;
        #pragma omp for schedule(static)
        for (Id row = 0; row < n; ++row) {
            double distance2 = 0;
            for (int a = 0; a < 3; ++a) {
                const Id i = 3*row+a;
                const double delta = points[i]-reference[i];
                distance2 += delta*delta;
                local_extent = std::max(local_extent, std::abs(points[i]));
            }
            local_motion2 = std::max(local_motion2, distance2);
        }
        // MSVC's OpenMP 2.0 mode has no max reduction clause.
        #pragma omp critical(blk_motion_merge)
        {
            motion2 = std::max(motion2, local_motion2);
            extent = std::max(extent, local_extent);
        }
    }
    const double margin = 128*std::numeric_limits<double>::epsilon()*(1+radius+extent);
    return std::sqrt(motion2)+cap < radius-margin;
}
BLK_EXPORT int blk_index_gather_plan(void *handle, const Id *ids, Id n, std::uint8_t *needed) {
    return guarded([&] {
        const auto &index = *static_cast<Index *>(handle);
        std::fill(needed, needed+index.cells.size(), 0);
        int invalid = 0;
        #pragma omp parallel num_threads(index.workers) reduction(|:invalid)
        {
            std::vector<std::uint8_t> local(index.cells.size(), 0);
            #pragma omp for schedule(static)
            for (Id row = 0; row < n; ++row) {
                const Id id = ids[row];
                if (id == index.offsets.back()) continue;
                if (id < 0 || id >= index.offsets.back()) { invalid = 1; continue; }
                const auto tile = std::upper_bound(index.offsets.begin()+1, index.offsets.end(), id)-index.offsets.begin()-1;
                local[tile] = 1;
            }
            #pragma omp critical(blk_plan_merge)
            {
                for (std::size_t tile = 0; tile < local.size(); ++tile) needed[tile] |= local[tile];
            }
        }
        if (invalid) throw std::runtime_error("Invalid map neighbor index in native gather plan");
    });
}
BLK_EXPORT int blk_index_query(void *handle, const double *points, Id n, int k, double cap,
                               const std::uint8_t *selected, double *distances, Id *ids) {
    return guarded([&] {
        const auto &index = *static_cast<Index *>(handle);
        // Spatially ordered submaps contain regions with very different query
        // costs. Work stealing changes only scheduling, never row results.
        #pragma omp parallel for num_threads(index.workers) schedule(dynamic, 256)
        for (Id row = 0; row < n; ++row) {
            double *d = distances + row*k;
            Id *out = ids + row*k;
            if (selected && !selected[row]) {
                std::fill(d, d+k, infinity); std::fill(out, out+k, index.offsets.back());
            } else {
                search_neighbors(index, points+3*row, k, cap, d, out);
            }
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

// Update certified-query state in place rather than allocating/scattering six
// large NumPy subsets per iteration. The query already resolved exact ties.
BLK_EXPORT void blk_correspondence_update(void *handle, const double *points,
    const Id *selected, Id n, const double *query_distances, const Id *query_ids, Id stride,
    double cap, double *reference, double *candidates, Id *candidate_ids, double *second,
    double *distances, Id *ids) {
    const auto &index = *static_cast<Index *>(handle);
    #pragma omp parallel for num_threads(index.workers) schedule(static)
    for (Id i = 0; i < n; ++i) {
        const Id row = selected[i];
        distances[row] = query_distances[row*stride];
        ids[row] = candidate_ids[row] = query_ids[row*stride];
        second[row] = std::min(query_distances[row*stride+1], cap);
        for (int a = 0; a < 3; ++a) {
            reference[3*row+a] = points[3*row+a];
            candidates[3*row+a] = std::numeric_limits<double>::quiet_NaN();
        }
    }
}
BLK_EXPORT void blk_correspondence_pack(void *handle, const Id *rows, Id n,
    const double *points, double *candidates, std::int32_t *pairs) {
    const auto &index = *static_cast<Index *>(handle);
    #pragma omp parallel for num_threads(index.workers) schedule(static)
    for (Id i = 0; i < n; ++i) {
        const Id row = rows[i];
        for (int a = 0; a < 3; ++a) candidates[3*row+a] = points[3*row+a];
        pairs[2*i] = pairs[2*i+1] = static_cast<std::int32_t>(row);
    }
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

BLK_EXPORT void *blk_matches_create(void *index, Id count) {
    try { return new MatchState(*static_cast<Index *>(index), count); }
    catch (const std::exception &e) { error = e.what(); return nullptr; }
}
BLK_EXPORT void blk_matches_destroy(void *handle) { delete static_cast<MatchState *>(handle); }
BLK_EXPORT int blk_matches_run(void *handle, const double *points, double cap, int reset,
    double *xyz, double *normals, double *distances, std::int32_t *pairs,
    std::uint8_t *ties, Id *statistics) {
    return guarded([&] {
        auto &state = *static_cast<MatchState *>(handle);
        const auto &index = state.index;
        const Id absent_id = index.offsets.back();
        Id queried = 0, reused = 0, absent = 0, tie_count = 0;
        int missing_tile = 0;
        const bool initialized = state.initialized && !reset;
        #pragma omp parallel for num_threads(index.workers) schedule(dynamic,256) reduction(+:queried,reused,absent,tie_count) reduction(|:missing_tile)
        for (Id row = 0; row < state.count; ++row) {
            const double *p = points+3*row;
            ties[row] = 0;
            distances[row] = infinity;
            Id seed_ids[2] = {absent_id,absent_id};
            double seed_distances2[2] = {infinity,infinity};
            if (initialized) {
                double motion2 = 0, distance2 = 0, p2 = 0, r2 = 0;
                for (int a = 0; a < 3; ++a) {
                    const double r = state.reference[3*row+a];
                    const double motion = p[a]-r, delta = p[a]-xyz[3*row+a];
                    motion2 += motion*motion; distance2 += delta*delta;
                    p2 += p[a]*p[a]; r2 += r*r;
                }
                const double movement = std::sqrt(motion2), distance = std::sqrt(distance2);
                const double margin = 128*std::numeric_limits<double>::epsilon()*(1+cap+state.second[row]+std::sqrt(p2)+std::sqrt(r2));
                const bool present = state.candidate_ids[row] < absent_id;
                if (present && distance+movement < state.second[row]-margin) {
                    if (distance < cap) { distances[row] = distance; ++reused; }
                    else ++absent;
                    continue;
                }
                if (!present && state.second[row]-movement > cap+margin) { ++absent; continue; }
                // Previous neighbors remain real map points. Their distances
                // give an upper bound before visiting a tree; no search radius
                // or possible better match is discarded.
                seed_ids[0] = state.candidate_ids[row]; seed_distances2[0] = distance2;
                seed_ids[1] = state.second_ids[row];
                if (seed_ids[1] < absent_id) {
                    seed_distances2[1] = 0;
                    for (int a = 0; a < 3; ++a) {
                        const double delta = p[a]-state.second_xyz[3*row+a];
                        seed_distances2[1] += delta*delta;
                    }
                }
            }
            double d[2]; Id ids[2];
            search_neighbors(index, p, 2, cap, d, ids,seed_ids,seed_distances2);
            ++queried;
            distances[row] = d[0];
            state.candidate_ids[row] = ids[0];
            state.second_ids[row] = ids[1];
            state.second[row] = std::min(d[1], cap);
            for (int a = 0; a < 3; ++a) state.reference[3*row+a] = p[a];
            if (ids[0] < absent_id) {
                if (!gather_row(index, ids[0], xyz+3*row, normals+3*row)) missing_tile = 1;
                if (std::isfinite(d[1]) && d[0] == d[1]) { ties[row] = 1; ++tie_count; }
            }
            if (ids[1] < absent_id && !gather_row(index,ids[1],state.second_xyz.data()+3*row,nullptr))
                missing_tile = 1;
        }
        if (missing_tile) throw std::runtime_error("Missing tile/normal buffer in fused correspondence query");
        // Compact in source order, independent of thread scheduling. The serial
        // Open3D Hessian reduction therefore receives the same ordered pairs.
        constexpr Id block_size = 4096;
        const Id blocks = (state.count+block_size-1)/block_size;
        std::vector<Id> starts(blocks+1, 0);
        #pragma omp parallel for num_threads(index.workers) schedule(static)
        for (Id block = 0; block < blocks; ++block) {
            for (Id row = block*block_size; row < std::min(state.count,(block+1)*block_size); ++row)
                starts[block+1] += distances[row] < cap;
        }
        for (Id block = 0; block < blocks; ++block) starts[block+1] += starts[block];
        #pragma omp parallel for num_threads(index.workers) schedule(static)
        for (Id block = 0; block < blocks; ++block) {
            Id offset = starts[block];
            for (Id row = block*block_size; row < std::min(state.count,(block+1)*block_size); ++row)
                if (distances[row] < cap) {
                    pairs[2*offset] = pairs[2*offset+1] = static_cast<std::int32_t>(row);
                    ++offset;
                }
        }
        statistics[0] = starts.back(); statistics[1] = queried;
        statistics[2] = reused; statistics[3] = absent; statistics[4] = tie_count;
        state.initialized = true;
    });
}
BLK_EXPORT int blk_matches_patch_ties(void *handle, const Id *rows, const Id *ids,
    const double *resolved_distances, Id n, double *xyz, double *normals, double *distances) {
    return guarded([&] {
        auto &state = *static_cast<MatchState *>(handle);
        for (Id i = 0; i < n; ++i) {
            const Id row = rows[i];
            if (!gather_row(state.index, ids[i], xyz+3*row, normals+3*row))
                throw std::runtime_error("Missing reference tie neighbor in fused query");
            state.candidate_ids[row] = ids[i];
            distances[row] = resolved_distances[i];
        }
    });
}
