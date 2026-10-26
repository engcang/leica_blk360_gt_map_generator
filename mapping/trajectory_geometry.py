"""Fixed-map geometric fitting and directional observability diagnostics."""
from pathlib import Path
import numpy as np
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation
from trajectory_io import digest, save_json, transform

def voxel(points: np.ndarray, size: float) -> np.ndarray:
    """Keep actual points in deterministic voxel order."""
    points = points[np.isfinite(points).all(axis=1)]
    cells = np.floor(points / size).astype(np.int64)
    if not len(cells):
        return points.astype(np.float32)
    if cells.min() >= -(1 << 20) and cells.max() < (1 << 20):
        # Monotone packed keys preserve the original XYZ lexicographic order
        # and first-point selection, without slow structured-row sorting.
        shifted = cells + (1 << 20)
        keys = (shifted[:, 0] << 42) | (shifted[:, 1] << 21) | shifted[:, 2]
        _, ids = np.unique(keys, return_index=True)
    else:
        _, ids = np.unique(cells, axis=0, return_index=True)
    return points[ids].astype(np.float32)


def ply_vertices(path: Path) -> np.ndarray:
    types = {'float': '<f4', 'double': '<f8', 'uchar': 'u1',
             'ushort': '<u2', 'uint': '<u4', 'int': '<i4'}
    with path.open('rb') as stream:
        if stream.readline().strip() != b'ply':
            raise ValueError('Not PLY')
        props, count, binary, vertex = [], None, False, False
        while True:
            line = stream.readline()
            if not line:
                raise ValueError('Incomplete PLY header')
            parts = line.decode('ascii').strip().split()
            if parts[:1] == ['format']:
                binary = parts[1] == 'binary_little_endian'
            elif parts[:2] == ['element', 'vertex']:
                count, vertex = int(parts[2]), True
            elif parts[:1] == ['element']:
                vertex = False
            elif parts[:1] == ['property'] and vertex:
                props.append((parts[2], types[parts[1]]))
            elif parts == ['end_header']:
                break
        offset = stream.tell()
    dtype = np.dtype(props)
    if not binary or count is None or not {'x', 'y', 'z'} <= set(dtype.names):
        raise ValueError('Expected binary XYZ PLY')
    if path.stat().st_size < offset + count * dtype.itemsize:
        raise ValueError('Truncated PLY')
    return np.memmap(path, mode='r', dtype=dtype, offset=offset, shape=(count,))


class Matcher:
    def __init__(self, cache: Path, workers: int) -> None:
        self.p = np.load(cache / 'points.npy', mmap_mode='r')
        self.n = np.load(cache / 'normals.npy', mmap_mode='r')
        self.good = np.load(cache / 'normal_valid.npy', mmap_mode='r')
        self.tree = cKDTree(self.p)
        self.workers = workers

    def correspondences(self, points: np.ndarray, T: np.ndarray, cap: float) -> tuple:
        moved = transform(points, T)
        dist, ids = self.tree.query(moved, distance_upper_bound=cap, workers=self.workers)
        valid = np.isfinite(dist)
        valid[valid] &= self.good[ids[valid]]
        n = self.n[ids[valid]]
        r = np.einsum('ij,ij->i', moved[valid] - self.p[ids[valid]], n)
        return moved[valid], n, r, valid, dist

    def icp(self, points: np.ndarray, initial: np.ndarray) -> np.ndarray:
        T = initial.copy()
        for cap in (.8, .4, .2):
            for _ in range(15):
                p, n, r, use, _ = self.correspondences(points, T, cap)
                if len(r) < 80:
                    break
                # Increment rotates about the current sensor origin, avoiding
                # an artificial lever arm from the arbitrary map origin.
                J = np.column_stack([np.cross(p - T[:3, 3], n), n])
                w = np.minimum(1., .05 / np.maximum(np.abs(r), 1e-12))
                H = J.T @ (w[:, None] * J)
                eig, vec = np.linalg.eigh(H)
                inv = np.where(eig > max(eig[-1] * 1e-7, 1e-8),
                               1. / np.maximum(eig, 1e-12), 0.)
                dx = -(vec * inv) @ (vec.T @ (J.T @ (w * r)))
                dx *= min(1., .1 / max(np.linalg.norm(dx[:3]), 1e-9),
                          .3 / max(np.linalg.norm(dx[3:]), 1e-9))
                # Fixed-correspondence capped-Huber line search; outliers do
                # not vanish from the candidate objective.
                rho = lambda a: np.where(a <= .05, .5*a*a, .05*(a-.025))
                old = rho(np.minimum(np.abs(r), cap)).sum()
                accepted = False
                center = T[:3, 3].copy()
                for scale in (1., .5, .25, .125):
                    R = Rotation.from_rotvec(dx[:3] * scale).as_matrix()
                    displacement = (p-center) @ R.T + center + dx[3:]*scale - p
                    new_r = r + np.einsum('ij,ij->i', n, displacement)
                    if rho(np.minimum(np.abs(new_r), cap)).sum() <= old:
                        T[:3, :3] = R @ T[:3, :3]
                        T[:3, 3] += dx[3:] * scale
                        accepted = True
                        break
                if not accepted or np.linalg.norm(dx) < 1e-5:
                    break
        return T

    def quality(self, points: np.ndarray, T: np.ndarray) -> dict:
        p, n, r, use, dist = self.correspondences(points, T, .20)
        return correspondence_quality(p, n, r, dist, T)


def correspondence_quality(p, n, r, dist, T):
    if len(r) < 80:
        return dict(accepted=False, reason='insufficient_support', points=len(r))
    J = np.column_stack([np.cross((p-T[:3, 3])/5., n), n])
    information = J.T @ J / len(J)
    eig, vectors = np.linalg.eigh(information)
    rank = int(np.count_nonzero(eig > max(eig[-1]*1e-3, 1e-5)))
    coverage = float(np.mean(dist < .20))
    rms = float(np.sqrt(np.mean(r*r)))
    accepted = coverage >= .65 and rms < .08 and rank == 6
    reason = 'geometry_pass' if accepted else ('insufficient_overlap' if coverage < .65 else 'surface_residual' if rms >= .08 else 'weak_directions')
    supported_distances = dist[np.isfinite(dist)]
    weak = vectors[:, eig <= max(eig[-1]*1e-3, 1e-5)]
    return dict(accepted=bool(accepted), reason=reason,
                points=len(r), coverage_20cm=coverage, plane_rms_m=rms,
                inlier_distance_mse_m2=float(np.mean(supported_distances**2)),
                inlier_distance_p90_m=float(np.quantile(supported_distances, .9)),
                observable_directions=rank, scaled_information_eigenvalues=eig.tolist(),
                scaled_information_matrix=information.tolist(), weak_directions=weak.T.tolist(),
                support_extent_m=np.ptp(p, axis=0).tolist())
