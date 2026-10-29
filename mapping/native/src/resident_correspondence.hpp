// Exact, double-precision matching for resident PLY registration clouds.
// Included by correspondence.cpp to share its result set and error handling.
namespace {
struct ResidentIndex {
    const double *points;
    Id count;
    int workers;
    using Metric = nanoflann::L2_Simple_Adaptor<double, ResidentIndex, double>;
    using Tree = nanoflann::KDTreeSingleIndexAdaptor<Metric, ResidentIndex, 3, std::uint32_t>;
    Tree tree;
    std::unique_ptr<PackedKdTree<Tree,double>> packed;
    ResidentIndex(const double *p, Id n, int threads)
        : points(p), count(tile_count(n)), workers(threads), tree(3,*this,{16}) {
        if (n >= std::numeric_limits<std::int32_t>::max())
            throw std::length_error("PLY target exceeds Open3D correspondence capacity");
        tree.buildIndex();
        packed = std::make_unique<PackedKdTree<Tree,double>>(tree);
    }
    std::size_t kdtree_get_point_count() const { return count; }
    double kdtree_get_pt(Id i, std::size_t d) const { return points[3*i+d]; }
    template<class Box> bool kdtree_get_bbox(Box &) const { return false; }
    double distance2(const double *p, Id id) const {
        double d = 0;
        for (int a = 0; a < 3; ++a) { const double delta = p[a]-points[3*id+a]; d += delta*delta; }
        return d;
    }
};
struct ResidentMatches {
    ResidentIndex &index;
    Id count;
    bool initialized = false;
    std::vector<double> reference, second;
    std::vector<std::uint32_t> first_ids, second_ids;
    ResidentMatches(ResidentIndex &i, Id n) : index(i), count(n), reference(3*n), second(n),
        first_ids(n,i.count), second_ids(n,i.count) {
        if (n < 0 || n >= std::numeric_limits<std::int32_t>::max())
            throw std::length_error("PLY source exceeds Open3D correspondence capacity");
    }
};
}

BLK_EXPORT void *blk_resident_create(const double *points, Id count, int workers) {
    try { return new ResidentIndex(points,count,workers); }
    catch (const std::exception &e) { error = e.what(); return nullptr; }
}
BLK_EXPORT void blk_resident_destroy(void *handle) { delete static_cast<ResidentIndex *>(handle); }
BLK_EXPORT void *blk_resident_matches_create(void *handle, Id count) {
    try { return new ResidentMatches(*static_cast<ResidentIndex *>(handle),count); }
    catch (const std::exception &e) { error = e.what(); return nullptr; }
}
BLK_EXPORT void blk_resident_matches_destroy(void *handle) { delete static_cast<ResidentMatches *>(handle); }
BLK_EXPORT int blk_resident_matches_run(void *handle, const double *points, double cap, int reset,
    double *distances, std::int32_t *ids, std::uint8_t *ties, Id *statistics) {
    return guarded([&] {
        auto &state = *static_cast<ResidentMatches *>(handle);
        const auto &index = state.index;
        const bool initialized = state.initialized && !reset;
        Id queried = 0, reused = 0, absent = 0, tie_count = 0;
        #pragma omp parallel for num_threads(index.workers) schedule(dynamic,256) reduction(+:queried,reused,absent,tie_count)
        for (Id row = 0; row < state.count; ++row) {
            const double *p = points+3*row;
            distances[row] = infinity; ids[row] = std::int32_t(index.count); ties[row] = 0;
            double seed2[2] = {infinity,infinity};
            if (initialized) {
                double motion2 = 0, p2 = 0, r2 = 0;
                for (int a = 0; a < 3; ++a) {
                    const double r = state.reference[3*row+a], delta = p[a]-r;
                    motion2 += delta*delta; p2 += p[a]*p[a]; r2 += r*r;
                }
                const double movement = std::sqrt(motion2);
                const double margin = 128*std::numeric_limits<double>::epsilon()*
                    (1+cap+state.second[row]+std::sqrt(p2)+std::sqrt(r2));
                const bool present = state.first_ids[row] < index.count;
                if (!present && state.second[row]-movement > cap+margin) { ++absent; continue; }
                if (present) {
                    seed2[0] = index.distance2(p,state.first_ids[row]);
                    const double distance = std::sqrt(seed2[0]);
                    if (distance > cap+margin && state.second[row]-movement > cap+margin) { ++absent; continue; }
                    if (distance+movement < state.second[row]-margin) {
                        if (distance < cap) {
                            distances[row] = distance; ids[row] = state.first_ids[row]; ++reused;
                        } else ++absent;
                        continue;
                    }
                }
                if (state.second_ids[row] < index.count) seed2[1] = index.distance2(p,state.second_ids[row]);
            }
            const double radius = cap+match_halo;
            double d[2] = {infinity,infinity}; Id found[2] = {index.count,index.count};
            Neighbors result{2,0,radius*radius,0,d,found};
            if (initialized) {
                result.seeded = true;
                if (state.first_ids[row] < index.count) result.addPoint(seed2[0],state.first_ids[row]);
                if (state.second_ids[row] < index.count) result.addPoint(seed2[1],state.second_ids[row]);
            }
            index.packed->search(result,p); ++queried;
            d[0] = std::sqrt(d[0]); d[1] = std::sqrt(d[1]);
            state.first_ids[row] = std::uint32_t(found[0]);
            state.second_ids[row] = std::uint32_t(found[1]);
            state.second[row] = std::min(d[1],radius);
            for (int a = 0; a < 3; ++a) state.reference[3*row+a] = p[a];
            if (d[0] < cap) {
                distances[row] = d[0]; ids[row] = std::int32_t(found[0]);
                // Resolve equal-distance choices with the original SciPy tree.
                // A tied candidate can never pass the strict reuse certificate.
                if (d[0] == d[1]) { ties[row] = 1; ++tie_count; }
            }
        }
        statistics[0] = queried; statistics[1] = reused;
        statistics[2] = absent; statistics[3] = tie_count;
        state.initialized = true;
    });
}
