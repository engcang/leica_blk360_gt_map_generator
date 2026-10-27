"""Portable scan and trajectory inputs; every source is opened read-only."""
from dataclasses import dataclass
from pathlib import Path
import csv
import hashlib
import json
import lzma
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation, Slerp


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()


def save_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def rigid(value):
    T = np.asarray(value, dtype=float)
    if T.shape != (4, 4) or not np.isfinite(T).all() or not np.allclose(T[3], [0, 0, 0, 1], atol=1e-7):
        raise ValueError('Expected a finite 4 by 4 rigid transform')
    if not np.allclose(T[:3, :3].T @ T[:3, :3], np.eye(3), atol=1e-4) or abs(np.linalg.det(T[:3, :3]) - 1) > 1e-4:
        raise ValueError('Transform rotation must be orthonormal with determinant +1')
    return T


def transform(points, pose):
    return points @ pose[:3, :3].T + pose[:3, 3]


def urdf_transform(path, source, destination):
    parents = {}
    for joint in ET.parse(path).getroot().findall('joint'):
        origin = joint.find('origin')
        xyz = [float(x) for x in (origin.get('xyz', '0 0 0') if origin is not None else '0 0 0').split()]
        rpy = [float(x) for x in (origin.get('rpy', '0 0 0') if origin is not None else '0 0 0').split()]
        T = np.eye(4)
        T[:3, :3] = Rotation.from_euler('xyz', rpy).as_matrix()
        T[:3, 3] = xyz
        parents[joint.find('child').get('link')] = (joint.find('parent').get('link'), T)
    def ancestry(link):
        visited, T = set(), np.eye(4)
        while link in parents:
            if link in visited:
                raise ValueError('URDF contains a cycle')
            visited.add(link)
            link, step = parents[link]
            T = step @ T
        return link, T
    a, A = ancestry(source)
    b, B = ancestry(destination)
    if a != b:
        raise ValueError('URDF frames are disconnected or missing')
    return rigid(np.linalg.inv(B) @ A)


class PoseGap(ValueError):
    pass


class Trajectory:
    """Normalize TUM, column-mapped CSV, or KITTI into world-from-body poses."""
    def __init__(self, config, max_gap=.5):
        self.config = dict(config)
        path = Path(config['path']).expanduser().resolve(strict=True)
        fmt = config.get('format', 'tum')
        self.files = [path]
        if fmt == 'csv':
            columns = config.get('columns') or dict(zip(('t', 'x', 'y', 'z', 'qx', 'qy', 'qz', 'qw'), ('timestamp', 'x', 'y', 'z', 'qx', 'qy', 'qz', 'qw')))
            with path.open(newline='', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f, delimiter=config.get('delimiter', ','))
                rows = [[float(row[columns[k]]) for k in ('t', 'x', 'y', 'z', 'qx', 'qy', 'qz', 'qw')] for row in reader]
            data = np.asarray(rows, dtype=float).reshape(-1, 8)
        elif fmt == 'tum':
            data = np.loadtxt(path, ndmin=2)
            if data.shape[1] != 8:
                raise ValueError('TUM rows must be: timestamp x y z qx qy qz qw')
        elif fmt == 'kitti':
            raw = np.loadtxt(path, ndmin=2)
            stamps_path = Path(config['timestamps']).expanduser().resolve(strict=True)
            self.files.append(stamps_path)
            stamps = np.loadtxt(stamps_path, ndmin=1)
            if raw.shape[1] != 12 or stamps.shape != (len(raw),):
                raise ValueError('KITTI needs one 3 by 4 matrix and one timestamp per pose')
            matrices = np.tile(np.eye(4), (len(raw), 1, 1))
            matrices[:, :3] = raw.reshape(-1, 3, 4)
            for T in matrices:
                rigid(T)
            data = np.column_stack((stamps, matrices[:, :3, 3], Rotation.from_matrix(matrices[:, :3, :3]).as_quat()))
        else:
            raise ValueError('Trajectory format must be tum, csv, or kitti')
        if len(data) < 2 or not np.isfinite(data).all():
            raise ValueError('Trajectory needs at least two finite poses')
        if float(config.get('time_scale', 1)) <= 0 or float(config.get('position_scale', 1)) <= 0:
            raise ValueError('Timestamp and position scales must be positive')
        data[:, 0] = data[:, 0] * float(config.get('time_scale', 1)) + float(config.get('time_offset_s', 0))
        data[:, 1:4] *= float(config.get('position_scale', 1))
        if np.any(np.diff(data[:, 0]) <= 0) or max_gap <= 0:
            raise ValueError('Trajectory timestamps must strictly increase; maximum gap must be positive')
        lengths = np.linalg.norm(data[:, 4:8], axis=1)
        if np.any(abs(lengths - 1) > .01):
            raise ValueError('Trajectory contains invalid quaternion norms')
        data[:, 4:8] /= lengths[:, None]
        matrices = np.tile(np.eye(4), (len(data), 1, 1))
        matrices[:, :3, :3] = Rotation.from_quat(data[:, 4:8]).as_matrix()
        matrices[:, :3, 3] = data[:, 1:4]
        if config.get('inverse', False):
            matrices = np.linalg.inv(matrices)
        # T_pose_from_body converts camera/sensor poses into the chosen body frame.
        matrices = matrices @ rigid(config.get('T_pose_from_body', np.eye(4)))
        self.data = np.column_stack((data[:, 0], matrices[:, :3, 3], Rotation.from_matrix(matrices[:, :3, :3]).as_quat()))
        self.times, self.matrices = data[:, 0], matrices
        self.max_gap = max_gap
        self.tolerance = max(1e-9, 4 * float(np.spacing(abs(self.times).max())))
        self.slerp = Slerp(self.times, Rotation.from_matrix(matrices[:, :3, :3]))

    def pose(self, stamps):
        t = np.atleast_1d(np.asarray(stamps, float)).copy()
        if not np.isfinite(t).all():
            raise PoseGap('Non-finite scan timestamp')
        right = np.clip(np.searchsorted(self.times, t), 0, len(self.times)-1)
        left = np.maximum(right-1, 0)
        nearest = np.where(abs(t-self.times[left]) < abs(t-self.times[right]), left, right)
        same = abs(t-self.times[nearest]) <= self.tolerance
        t[same] = self.times[nearest[same]]
        if np.any((t < self.times[0]) | (t > self.times[-1])):
            raise PoseGap('Scan lies outside estimated trajectory time support')
        k = np.clip(np.searchsorted(self.times, t, side='right')-1, 0, len(self.times)-2)
        if np.any((self.times[k+1]-self.times[k] > self.max_gap+self.tolerance) & (t > self.times[k]) & (t < self.times[k+1])):
            raise PoseGap('Estimated trajectory gap prevents deskew')
        out = np.tile(np.eye(4), (len(t), 1, 1))
        out[:, :3, :3] = self.slerp(t).as_matrix()
        out[:, :3, 3] = np.column_stack([np.interp(t, self.times, self.matrices[:, j, 3]) for j in range(3)])
        return out


@dataclass
class Scan:
    index: int
    timestamp: float
    points: np.ndarray
    frontend_T: np.ndarray


class ScanSource:
    def __init__(self, config, trajectory):
        self.config, self.frontend = dict(config), trajectory
        self.path = Path(config['path']).expanduser().resolve(strict=True)
        self.sequence = self.path.name
        self.ext = rigid(config.get('T_body_from_sensor', np.eye(4)))
        self.min_range = float(config.get('min_range_m', 1))
        self.max_range = float(config.get('max_range_m', 60))
        if not 0 <= self.min_range < self.max_range:
            raise ValueError('Invalid scan range limits')
        self.scan_quality = {'skipped_scan_intervals': []}
        self._skipped = set()
        self.files = list(trajectory.files)
        self.kind = config.get('format', 'directory')
        self.rows = []
        if self.kind == 'tartanstar':
            self._open_tartanstar()
        elif self.kind in ('manifest', 'directory'):
            if self.kind == 'manifest':
                self.files.append(self.path)
                document = json.loads(self.path.read_text(encoding='utf-8'))
                self.rows = document['scans']
                root = self.path.parent
            else:
                root = self.path
                for p in root.iterdir():
                    if p.suffix.lower() in ('.ply', '.npz', '.npy', '.bin', '.pcd'):
                        try:
                            self.rows.append({'timestamp': float(p.stem), 'path': p.name})
                        except ValueError:
                            continue
                self.rows.sort(key=lambda r: r['timestamp'])
            for row in self.rows:
                row['path'] = str((root / row['path']).resolve(strict=True))
                self.files.append(Path(row['path']))
            self.timestamps = np.asarray([r['timestamp'] for r in self.rows], float)
        else:
            raise ValueError('Scan format must be directory, manifest, or tartanstar')
        if not len(self.timestamps) or not np.isfinite(self.timestamps).all() or np.any(np.diff(self.timestamps) <= 0):
            raise ValueError('Scans need strictly increasing timestamps; use numeric filenames or a scan manifest')
        self.timestamps = self.timestamps * float(config.get('time_scale', 1)) + float(config.get('time_offset_s', 0))
        if np.any(np.diff(self.timestamps) <= 0):
            raise ValueError('Scan timestamp scale must be positive')
        self._anchor_timestamps = {}
        self.initial_stats = self.stats()
        self.provenance = {'format': self.kind, 'path': str(self.path), 'config': config,
                           'frontend_config': trajectory.config, 'frontend_sha256': digest(trajectory.files[0]),
                           'metadata_sha256': {str(p): digest(p) for p in self.files if p.suffix in ('.json', '.yaml', '.tum') or p.name == 'meta.npz'},
                           'file_stats': self.initial_stats}

    def _open_tartanstar(self):
        import yaml
        self.lidar = self.path / 'lidar' if (self.path / 'lidar').is_dir() else self.path
        meta_path, yaml_path = self.lidar / 'meta.npz', self.lidar / 'meta.yaml'
        with np.load(meta_path, allow_pickle=False) as z:
            self.timestamps = z['ts'].copy()
            self.offsets = {'rng': z['rng_offset'].copy(), 'tcol': z['ts_offset'].copy()}
        config = yaml.safe_load(yaml_path.read_text())
        if config.get('range_dtype', '<u4') != '<u4':
            raise ValueError('TartanStar scans require lossless uint32 ranges')
        info = config['sensor_info']
        beams = info['beam_intrinsics']
        self.width = int(info['lidar_data_format']['columns_per_frame'])
        self.height = int(info['lidar_data_format']['pixels_per_column'])
        alt = np.deg2rad(beams['beam_altitude_angles'])[:, None]
        azimuth = np.deg2rad(beams['beam_azimuth_angles'])[:, None]
        beam_T = rigid(np.asarray(beams['beam_to_lidar_transform']).reshape(4, 4))
        self.bx, self.bz = beam_T[0, 3], beam_T[2, 3]
        self.beam_r = np.hypot(self.bx, self.bz)
        encoder = 2*np.pi*(1-np.arange(self.width)/self.width)[None, :]
        self.direction = np.stack(np.broadcast_arrays(np.cos(alt)*np.cos(encoder-azimuth), np.cos(alt)*np.sin(encoder-azimuth), np.sin(alt)), axis=-1)
        self.offset = np.stack(np.broadcast_arrays(self.bx*np.cos(encoder), self.bx*np.sin(encoder), self.bz), axis=-1)
        for name, offsets in self.offsets.items():
            p = self.lidar/name
            if len(offsets) != len(self.timestamps)+1 or offsets[0] != 0 or np.any(np.diff(offsets.astype(np.int64)) <= 0) or int(offsets[-1]) != p.stat().st_size:
                raise ValueError('Invalid extracted LiDAR offsets: '+str(p))
            self.files.append(p)
        self.files += [meta_path, yaml_path]
        # Validate byte widths without importing the dataset SDK.
        for i in sorted({0, len(self.timestamps)//2, len(self.timestamps)-1}):
            self._decode('rng', i, '<u4', (self.height, self.width))

    def _decode(self, name, index, dtype, shape):
        begin, end = (int(x) for x in self.offsets[name][index:index+2])
        with (self.lidar/name).open('rb') as f:
            f.seek(begin)
            data = lzma.decompress(f.read(end-begin))
        if len(data) != np.prod(shape)*np.dtype(dtype).itemsize:
            raise ValueError(f'Invalid {name} byte count at scan {index}; lossless uint32 ranges are required')
        return np.frombuffer(data, dtype=dtype).reshape(shape)

    def _column_times(self, index):
        columns = self._decode('tcol', index, '<f8', (self.width,))
        valid = np.isfinite(columns) & (columns > 0)
        if not valid.any():
            raise PoseGap('No valid LiDAR column timestamps')
        columns = columns*float(self.config.get('time_scale', 1))+float(self.config.get('time_offset_s', 0))
        if np.ptp(columns[valid]) > .5:
            raise ValueError('LiDAR column clock span exceeds 0.5 seconds')
        return columns, valid, float(columns[valid].max())

    def anchor_timestamp(self, index):
        """Exact deskew/pose anchor, distinct from TartanStar selection stamps.

        Normal scan loading populates this cache. Resume validation reads only
        the small column-clock record when the scan has not been loaded yet.
        """
        index = int(index)
        if self.kind != 'tartanstar':
            return float(self.timestamps[index])
        if index not in self._anchor_timestamps:
            self._anchor_timestamps[index] = self._column_times(index)[2]
        return self._anchor_timestamps[index]

    def stats(self):
        return [[str(p), p.stat().st_size, p.stat().st_mtime_ns] for p in self.files]

    def verify_unchanged(self):
        if self.stats() != self.initial_stats:
            raise ValueError('Scan or trajectory input changed during processing')

    def pose(self, stamps):
        return self.frontend.pose(stamps)

    def selection(self, period=.1):
        if period <= 0:
            raise ValueError('Scan period must be positive')
        lo, hi = self.frontend.times[[0, -1]]
        ids = np.unique(np.searchsorted(self.timestamps, np.arange(max(lo, self.timestamps[0]), min(hi, self.timestamps[-1])+period*.001, period)))
        return ids[ids < len(self.timestamps)]

    def scan(self, index):
        index = int(index)
        stamp = float(self.timestamps[index])
        times = None
        if self.kind == 'tartanstar':
            ranges = self._decode('rng', index, '<u4', (self.height, self.width))
            columns, valid_columns, stamp = self._column_times(index)
            self._anchor_timestamps[index] = stamp
            xyz = ((ranges.astype(np.float32)-self.beam_r)[..., None]*self.direction+self.offset)*.001
            valid = (ranges > 0) & valid_columns[None, :]
            points = xyz[valid]
            times = np.broadcast_to(columns, ranges.shape)[valid]
        else:
            row = self.rows[index]
            path = Path(row['path'])
            if path.suffix.lower() == '.npz':
                with np.load(path, allow_pickle=False) as z:
                    points = z['points'].copy()
                    times = z['timestamps'].copy() if 'timestamps' in z else None
            elif path.suffix.lower() == '.npy':
                points = np.load(path, allow_pickle=False)
            elif path.suffix.lower() == '.bin':
                width = int(self.config.get('bin_columns', 4))
                if width not in (3, 4):
                    raise ValueError('Binary scans need 3 XYZ or 4 XYZI float32 columns')
                points = np.fromfile(path, dtype='<f4').reshape(-1, width)[:, :3]
            else:
                import open3d as o3d
                points = np.asarray(o3d.io.read_point_cloud(str(path)).points)
            if times is not None:
                times = np.asarray(times, float)*float(self.config.get('time_scale', 1))
                times += stamp if self.config.get('point_times_relative', False) else float(self.config.get('time_offset_s', 0))
        points = np.asarray(points, float) * float(self.config.get('point_scale', 1))
        if points.ndim != 2 or points.shape[1] != 3:
            raise ValueError('Scan points must have shape N by 3')
        radius = np.linalg.norm(points, axis=1)
        valid = np.isfinite(points).all(1) & (radius >= self.min_range) & (radius <= self.max_range)
        if times is not None:
            if times.shape != (len(points),):
                raise ValueError('Point timestamp count does not match the scan')
            valid &= np.isfinite(times)
        points = transform(points[valid], self.ext)
        anchor = self.pose([stamp])[0]
        if times is not None:
            unique, inverse = np.unique(times[valid], return_inverse=True)
            poses = self.pose(unique)
            world = np.einsum('nij,nj->ni', poses[inverse, :3, :3], points)+poses[inverse, :3, 3]
            points = (world-anchor[:3, 3]) @ anchor[:3, :3]
        if len(points) < 80:
            raise PoseGap('Fewer than 80 usable scan points')
        return Scan(index, stamp, points, anchor)

    def iter_scans(self, period_s=.1, scan_indices=None, after_index=-1):
        indices = self.selection(period_s) if scan_indices is None else scan_indices
        for i in indices:
            if i <= after_index:
                continue
            try:
                yield self.scan(i)
            except PoseGap as error:
                if int(i) not in self._skipped:
                    self._skipped.add(int(i))
                    self.scan_quality['skipped_scan_intervals'].append({'scan_index': int(i), 'timestamp': float(self.timestamps[i]), 'reason': str(error)})
        self.verify_unchanged()
