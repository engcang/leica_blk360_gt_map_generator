"""Validate local TLS anchors before trusting a map-from-odometry transform."""
import json
import time
from pathlib import Path

import numpy as np

from trajectory_geometry import voxel
from trajectory_io import PoseGap, rigid, save_json, transform
from trajectory_recovery import fit_scan, difference
from trajectory_validation import (VALIDATION_PROTOCOL, context_map_supported, map_rooted,
                                   confirm_partial_views, jointly_observable)

INITIALIZATION_PROTOCOL = 'local-window-direct-tls-v2'


def initialize(config, source, matcher, fine, output, indices, workers, progress):
    path = output/'initial_alignment.json'
    # The containing job's provenance binds input/map hashes and protocol.
    if path.exists():
        saved = json.loads(path.read_text())
        if (saved.get('protocol') == INITIALIZATION_PROTOCOL
                and saved.get('validation_protocol') == VALIDATION_PROTOCOL
                and saved.get('validated')):
            return rigid(saved['T_map_from_frontend'])
    supplied = config.get('initial_alignment')
    if isinstance(supplied, str):
        supplied = json.loads(Path(supplied).read_text())
    if isinstance(supplied, dict):
        supplied = supplied.get('T_map_from_frontend', supplied)
    supplied = rigid(supplied) if supplied is not None else None
    hypotheses = []
    targets = {}
    starts = source.timestamps[np.asarray(indices, int)]
    # Short windows do not bake whole-sequence SLAM drift into a rigid cloud.
    elapsed = starts-starts[0]
    positions = [0]+[min(len(indices)-1, int(np.searchsorted(elapsed, t)))
                     for t in (3, 10, 30, 60, 120, 180, 300)]
    positions += np.linspace(0, len(indices)-1, min(8, len(indices))).astype(int).tolist()

    def record(row):
        hypotheses.append(row)
        save_json(output/'alignment_diagnostics.json', dict(protocol=INITIALIZATION_PROTOCOL, hypotheses=hypotheses))

    def check(scans, anchor, initial, origin, validators=None):
        start = time.perf_counter()
        checks = [anchor]+[s for s in (scans if validators is None else validators)
                           if s.index != anchor.index]
        checks = checks[:3]
        fits = []
        # Refine each held-out scan independently, never accept a coarse fit.
        for scan in checks:
            guess = initial@np.linalg.inv(anchor.frontend_T)@scan.frontend_T
            T, q = fit_scan(fine, scan, guess)
            fits.append((scan, T, q))
            if not (q.get('fine',{}).get('converged') and q.get('geometry_supported')):
                record(dict(origin=origin, anchor_index=anchor.index, validated=False,
                            checks=[v[2] for v in fits], elapsed_s=time.perf_counter()-start))
                return None
        if len(fits) < 3:
            return None
        A = fits[0][1]@np.linalg.inv(anchor.frontend_T)
        errors = [difference(T, A@s.frontend_T) for s, T, _ in fits]
        confirm_partial_views([q for _,_,q in fits], source)
        valid = (all(q.get('accepted') and (map_rooted(q) or context_map_supported(q)) for _,_,q in fits)
                 and all(dm <= .15 and da <= 3 for dm, da in errors))
        shared = None
        if not valid and all(q.get('geometry_supported') for _,_,q in fits):
            training = [s for s in scans if s.index not in {v[0].index for v in fits}]
            if len(training) >= 2:
                points = voxel(np.concatenate([transform(voxel(s.points,.01),
                    np.linalg.inv(anchor.frontend_T)@s.frontend_T) for s in training]),.01)
                T, info = fine.align(points, initial, capture_information=True)
                tq = fine.quality(voxel(points,.1),T)
                if (info.get('converged') and context_map_supported(tq)
                        and info.get('optimizer_information',{}).get('observable_directions') == 6):
                    fits = [(s,*fit_scan(fine,s,T@np.linalg.inv(anchor.frontend_T)@s.frontend_T)) for s,_,_ in fits]
                predicted = [fine.quality(voxel(s.points,.1),T@np.linalg.inv(anchor.frontend_T)@s.frontend_T) for s,_,_ in fits]
                agreement = [difference(fit,T@np.linalg.inv(anchor.frontend_T)@s.frontend_T) for s,fit,_ in fits]
                joint = jointly_observable(predicted,
                    [T@np.linalg.inv(anchor.frontend_T)@s.frontend_T for s,_,_ in fits],T,
                    shared_transform_fitted=bool(info.get('converged')))
                valid = bool(context_map_supported(tq) and info.get('optimizer_information',{}).get('observable_directions') == 6
                             and joint['accepted'] and all(context_map_supported(q) and q['fine'].get('converged') for _,_,q in fits)
                             and all(dm <= .15 and da <= 3 for dm,da in agreement))
                shared = dict(fine=info,training_quality=tq,joint=joint,agreement_m_deg=agreement,
                              training_indices=[s.index for s in training])
                if valid:
                    A=T@np.linalg.inv(anchor.frontend_T)
        row = dict(protocol=INITIALIZATION_PROTOCOL, validation_protocol=VALIDATION_PROTOCOL,
                   origin=origin, anchor_index=anchor.index, validated=valid,
                   T_map_from_frontend=A.tolist(), checks=[q for _, _, q in fits],
                   independent_agreement_m_deg=errors, shared_validation=shared, elapsed_s=time.perf_counter()-start)
        record(row)
        if valid:
            save_json(path, row)
            return A
        return None

    for pos in dict.fromkeys(positions):
        center = starts[pos]
        for radius in (1., 3.):
            selected = np.asarray(indices)[abs(starts-center) <= radius]
            ids = selected[np.unique(np.linspace(0, len(selected)-1, min(9,len(selected))).astype(int))]
            scans = []
            for index in ids:
                try:
                    scans.append(source.scan(int(index)))
                except PoseGap:
                    continue
            if len(scans) < 3:
                continue
            anchor = min(scans, key=lambda s:abs(s.timestamp-center))
            if supplied is not None:
                A = check(scans, anchor, supplied@anchor.frontend_T, 'supplied-validated')
                supplied = None
                if A is not None:
                    return A
            # Keep three complete scans out of KISS training.
            validators = [anchor]+[s for s in scans[1::2] if s.index != anchor.index]
            validators = validators[:3]
            training = [s for s in scans if s.index not in {v.index for v in validators}]
            if len(validators) < 3 or not training:
                continue
            cloud = voxel(np.concatenate([transform(voxel(s.points,.1),
                np.linalg.inv(anchor.frontend_T)@s.frontend_T) for s in training]), .1)
            for resolution in (.3, .5):
                progress(f'Finding and validating initial TLS location: scan {anchor.index}, {2*radius:g} s window')
                if resolution not in targets:
                    targets[resolution] = voxel(matcher.p, resolution)
                try:
                    T = fine.native.kiss(voxel(cloud,resolution), targets[resolution], resolution, workers)
                    T = matcher.icp(cloud, T)
                    q = matcher.quality(voxel(anchor.points,.1), T)
                    if not q.get('geometry_supported') or q.get('coverage_20cm',0) < .40:
                        record(dict(origin='kiss', anchor_index=anchor.index, radius_s=radius,
                                    resolution_m=resolution, validated=False, coarse_quality=q))
                        continue
                    A = check(scans, anchor, T, f'kiss-{resolution:g}m-{2*radius:g}s', validators)
                    if A is not None:
                        return A
                except (ValueError, RuntimeError) as exc:
                    record(dict(origin='kiss', anchor_index=anchor.index, validated=False, error=str(exc)))
    raise ValueError('No initial location passed independent 1 cm TLS validation; inspect alignment_diagnostics.json')
