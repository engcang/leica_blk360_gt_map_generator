// The trajectory 10 cm Huber solver, with its existing update controls.
// Included after resident_correspondence.hpp; the map and exact search index
// are immutable. Scratch storage is reused between scans, not shared by jobs.
namespace {
using CoarseMatrix = Eigen::Matrix<double,6,6>;
using CoarseVector = Eigen::Matrix<double,6,1>;
struct CoarseBlock {
    CoarseMatrix h = CoarseMatrix::Zero();
    CoarseVector b = CoarseVector::Zero();
    double cost = 0;
    Id count = 0;
};
double coarse_rho(double value, double cap) {
    const double a = std::min(std::abs(value),cap);
    return a <= .05 ? .5*a*a : .05*(a-.025);
}
struct CoarseIcp {
    ResidentIndex &index;
    const double *normals;
    const std::uint8_t *good;
    ResidentMatches matches;
    std::vector<double> moved, distances, residuals;
    std::vector<std::int32_t> ids;
    std::vector<std::uint8_t> ties, valid;
    std::vector<CoarseBlock> blocks;
    std::vector<double> costs;
    static constexpr Id block_size = 512;
    CoarseIcp(ResidentIndex &i, const double *n, const std::uint8_t *g)
        : index(i), normals(n), good(g), matches(i,0) {}
    void resize(Id count) {
        if (count < 0 || count >= std::numeric_limits<std::int32_t>::max())
            throw std::length_error("Coarse source exceeds correspondence capacity");
        matches.count = count; matches.initialized = false;
        matches.reference.resize(3*count); matches.second.resize(count);
        matches.first_ids.resize(count); matches.second_ids.resize(count);
        moved.resize(3*count); distances.resize(count); residuals.resize(count);
        ids.resize(count); ties.resize(count); valid.resize(count);
        blocks.resize((count+block_size-1)/block_size); costs.resize(blocks.size());
    }
    void run(const double *points, Id count, const double *initial, double *output, Id *stats) {
        resize(count);
        std::fill(stats,stats+6,0);
        using Transform = Eigen::Matrix<double,4,4,Eigen::RowMajor>;
        Transform T = Eigen::Map<const Transform>(initial);
        for (const double cap : {.8,.4,.2}) for (int iteration=0; iteration<15; ++iteration) {
            ++stats[0];
            const Eigen::Matrix3d rotation = T.topLeftCorner<3,3>();
            const Eigen::Vector3d center = T.topRightCorner<3,1>();
            #pragma omp parallel for num_threads(index.workers) schedule(static)
            for (Id row=0; row<count; ++row) {
                const Eigen::Map<const Eigen::Vector3d> point(points+3*row);
                Eigen::Map<Eigen::Vector3d> target(moved.data()+3*row);
                target = rotation*point+center;
            }
            Id queries[4];
            if (blk_resident_matches_run(&matches,moved.data(),cap,0,distances.data(),
                    ids.data(),ties.data(),queries)) throw std::runtime_error(error);
            for (int i=0;i<4;++i) stats[i+1] += queries[i];
            // The reference SciPy tree owns equal-distance tie selection.
            // Retry the entire fit there, from the original initial transform.
            if (queries[3]) { stats[5]=1; return; }
            // Fixed blocks give the same reduction order at every thread count.
            #pragma omp parallel for num_threads(index.workers) schedule(static)
            for (Id block=0;block<Id(blocks.size());++block) {
                CoarseBlock sum;
                for (Id row=block*block_size;row<std::min(count,(block+1)*block_size);++row) {
                    valid[row] = distances[row]<cap && good[ids[row]];
                    if (!valid[row]) continue;
                    const Eigen::Map<const Eigen::Vector3d> p(moved.data()+3*row);
                    const Eigen::Map<const Eigen::Vector3d> n(normals+3*ids[row]);
                    const Eigen::Map<const Eigen::Vector3d> q(index.points+3*ids[row]);
                    const double r = (p-q).dot(n);
                    residuals[row] = r;
                    CoarseVector j; j.head<3>() = (p-center).cross(n); j.tail<3>()=n;
                    const double w = std::min(1.,.05/std::max(std::abs(r),1e-12));
                    sum.h.noalias() += j*(w*j).transpose();
                    sum.b.noalias() += j*(w*r);
                    sum.cost += coarse_rho(r,cap); ++sum.count;
                }
                blocks[block]=sum;
            }
            CoarseBlock total;
            for (const auto &block:blocks) {
                total.h+=block.h;total.b+=block.b;total.cost+=block.cost;total.count+=block.count;
            }
            if (total.count<80) break;
            Eigen::SelfAdjointEigenSolver<CoarseMatrix> solve(total.h);
            if (solve.info()!=Eigen::Success) throw std::runtime_error("Coarse eigensolve failed");
            const auto &eigenvalues=solve.eigenvalues();
            CoarseVector inv = CoarseVector::Zero();
            const double threshold=std::max(eigenvalues[5]*1e-7,1e-8);
            for (int i=0;i<6;++i) if (eigenvalues[i]>threshold) inv[i]=1./std::max(eigenvalues[i],1e-12);
            CoarseVector dx = -solve.eigenvectors()*inv.asDiagonal()*(solve.eigenvectors().transpose()*total.b);
            dx *= std::min({1.,.1/std::max(dx.head<3>().norm(),1e-9),.3/std::max(dx.tail<3>().norm(),1e-9)});
            bool accepted=false;
            for (const double scale:{1.,.5,.25,.125}) {
                const Eigen::Vector3d angle=dx.head<3>()*scale;
                const double length=angle.norm();
                const Eigen::Matrix3d R=length>0 ? Eigen::AngleAxisd(length,angle/length).toRotationMatrix()
                                               : Eigen::Matrix3d::Identity();
                const Eigen::Vector3d translation=dx.tail<3>()*scale;
                #pragma omp parallel for num_threads(index.workers) schedule(static)
                for (Id block=0;block<Id(blocks.size());++block) {
                    double cost=0;
                    for (Id row=block*block_size;row<std::min(count,(block+1)*block_size);++row) {
                        if (!valid[row]) continue;
                        const Eigen::Map<const Eigen::Vector3d> p(moved.data()+3*row);
                        const Eigen::Map<const Eigen::Vector3d> n(normals+3*ids[row]);
                        const Eigen::Vector3d displacement=R*(p-center)+center+translation-p;
                        cost+=coarse_rho(residuals[row]+n.dot(displacement),cap);
                    }
                    costs[block]=cost;
                }
                double cost=0;for (double part:costs) cost+=part;
                if (cost<=total.cost) {
                    T.topLeftCorner<3,3>()=R*rotation;
                    T.topRightCorner<3,1>()=center+translation;
                    accepted=true;break;
                }
            }
            if (!accepted || dx.norm()<1e-5) break;
        }
        Eigen::Map<Transform> result(output);result=T;
    }
};
}
BLK_EXPORT void *blk_coarse_create(void *index, const double *normals, const std::uint8_t *valid) {
    try { return new CoarseIcp(*static_cast<ResidentIndex *>(index),normals,valid); }
    catch(const std::exception &e) { error=e.what();return nullptr; }
}
BLK_EXPORT void blk_coarse_destroy(void *handle) { delete static_cast<CoarseIcp *>(handle); }
BLK_EXPORT int blk_coarse_run(void *handle, const double *points, Id count, const double *initial,
                            double *output, Id *statistics) {
    return guarded([&] { static_cast<CoarseIcp *>(handle)->run(points,count,initial,output,statistics); });
}
