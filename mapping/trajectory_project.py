"""Sequence discovery, batch budgets, and explicitly opened trajectory results."""
from pathlib import Path
import argparse
import json

from parallel_runtime import threadBudget, memoryWorkers

GT_NAME = 'ground_truth_trajectory.tum'
WORK_NAME = '.ground_truth'
SCAN_EXTENSIONS = {'.ply', '.pcd', '.npy', '.npz', '.bin'}


def estimate_files(folder):
    """Look only in the selected sequence and conventional odometry folders."""
    files = []
    for directory in [folder] + [folder/name for name in ('odometry', 'slam', 'trajectory', 'trajectories', 'poses')]:
        if not directory.is_dir():
            continue
        files.extend(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in ('.tum', '.txt', '.csv')
                     and p.name not in (GT_NAME, 'ground_truth_trajectory.partial.tum', 'candidate_trajectory.tum', 'timestamps.txt', 'times.txt'))
    def priority(p):
        name=p.stem.lower()
        return (0 if any(s in name for s in ('frontend','odometry','odom')) else 2 if 'backend' in name else 1, str(p))
    result=[]
    for path in sorted(files, key=priority):
        try:
            with path.open(encoding='utf-8-sig') as stream:
                first=next(line.strip() for line in stream if line.strip() and not line.lstrip().startswith('#'))
            if path.suffix.lower()=='.csv':
                delimiter=';' if ';' in first else ','
                columns=first.split(delimiter)
                if not {'timestamp','x','y','z','qx','qy','qz','qw'}.issubset(columns):
                    continue
                value=dict(path=str(path),format='csv',delimiter=delimiter)
            else:
                numbers=[float(x) for x in first.split()]
                if len(numbers)==8:
                    value=dict(path=str(path),format='tum')
                elif len(numbers)==12:
                    stamps=next((path.parent/n for n in ('timestamps.txt','times.txt') if (path.parent/n).is_file()),None)
                    if stamps is None: continue
                    value=dict(path=str(path),format='kitti',timestamps=str(stamps))
                else: continue
            value['label']=path.stem
            result.append(value)
        except (OSError, ValueError, StopIteration):
            continue
    return result


def discover(folder):
    folder=Path(folder).expanduser().resolve(strict=True)
    if not folder.is_dir(): raise ValueError('Choose a sequence folder')
    scan=dict(path='',format='directory',time_scale=1)
    if (folder/'lidar/meta.npz').is_file():
        scan.update(path=str(folder),format='tartanstar',calibration_preset='tartanstar')
    else:
        for directory in [folder/name for name in ('scans','lidar','velodyne','pointclouds','ply')] + [folder]:
            if not directory.is_dir(): continue
            numeric=False
            for p in directory.iterdir():
                if p.is_file() and p.suffix.lower() in SCAN_EXTENSIONS:
                    try: float(p.stem);numeric=True;break
                    except ValueError: pass
            if numeric:
                scan['path']=str(directory);break
        if not scan['path']:
            for name in ('scans.json','scan_manifest.json','manifest.json'):
                p=folder/name
                try:
                    if isinstance(json.loads(p.read_text()).get('scans'),list):
                        scan.update(path=str(p),format='manifest');break
                except (OSError,ValueError,AttributeError):pass
    return dict(name=folder.name,sequence_directory=str(folder),scans=scan,trajectories=estimate_files(folder),
                output_directory=str(folder/WORK_NAME),trajectory_output=str(folder/GT_NAME),period_s=0)


def batch_plan(configs, workers=0):
    if not configs: raise ValueError('Add at least one sequence')
    cpus=threadBudget(workers)
    slots=memoryWorkers(max(1,cpus//4),len(configs),8*1024**3)
    outputs=set()
    for config in configs:
        output=str(Path(config['output_directory']).expanduser().resolve())
        if output in outputs: raise ValueError('A sequence appears more than once in this batch')
        outputs.add(output)
    return dict(concurrency=slots,workers_per_sequence=max(1,cpus//slots),cpu_budget=cpus)


def read_result(path):
    """Open a saved scene when present; otherwise display TUM and nearby odometry."""
    from trajectory_io import Trajectory
    path=Path(path).expanduser().resolve(strict=True)
    folder=path if path.is_dir() else path.parent
    candidates=[folder,folder/WORK_NAME] if path.is_dir() or path.name==GT_NAME else []
    for work in candidates:
        if (work/'viewer.json').is_file():
            scene=json.loads((work/'viewer.json').read_text())
            if scene.get('schema_version')!=1: raise ValueError('Unsupported trajectory result')
            if path.is_file():
                from trajectory_io import digest
                if scene.get('trajectory_sha256') != digest(path):
                    continue
            return dict(output_directory=str(work),name=scene.get('name',folder.name))
        if (work/'job_status.json').is_file():
            status = json.loads((work/'job_status.json').read_text())
            if status.get('status') in ('queued','running','failed','cancelled','interrupted'):
                return dict(output_directory=str(work), name=folder.name)
    gt=folder/GT_NAME if path.is_dir() else path
    if not gt.is_file(): raise ValueError(f'No {GT_NAME} or saved viewer in {folder}')
    trajectory=Trajectory(dict(path=str(gt)),max_gap=.5)
    layers=[dict(key='tls_gt',label='Generated ground-truth trajectory',color='#FF9B54',rows=trajectory.data.tolist())]
    estimates=estimate_files(folder)
    # Apply a saved map alignment when available; never silently invent one.
    alignment=None
    for file in (folder/WORK_NAME/'baseline/initial_alignment.json', folder/'baseline/initial_alignment.json'):
        if file.is_file():
            alignment=json.loads(file.read_text()).get('T_map_from_frontend');break
    for value in estimates:
        if Path(value['path'])==gt: continue
        try:
            estimated=Trajectory(value)
            rows=estimated.data
            if alignment is not None:
                import numpy as np
                from scipy.spatial.transform import Rotation
                matrices=np.asarray(alignment)@estimated.matrices
                rows=np.column_stack((estimated.times,matrices[:,:3,3],Rotation.from_matrix(matrices[:,:3,:3]).as_quat()))
            layers.append(dict(key=f'estimate_{len(layers)-1}',label=value['label']+(' · original coordinates' if alignment is None else ''),color='#5EC8DE' if len(layers)==1 else '#CB8CFF',rows=rows.tolist()))
        except (OSError,ValueError,KeyError):continue
    scene=dict(schema_version=1,name=folder.name,maps=[],trajectories=layers,quality=[],gap_s=.5,
               input_quality={},estimate_alignment='saved' if alignment is not None else 'original coordinates')
    return dict(output_directory=str(folder),name=folder.name,scene=scene,source_file=str(gt))


def open_results(paths):
    def available(folder):
        return any((folder/name).is_file() for name in (GT_NAME,'viewer.json','job_status.json')) or (folder/WORK_NAME/'viewer.json').is_file()
    results=[]
    for selected in paths:
        path=Path(selected).expanduser().resolve(strict=True)
        if path.is_dir() and not available(path):
            children=[p for p in sorted(path.iterdir()) if p.is_dir() and
                      available(p)]
            if children: results.extend(read_result(p) for p in children);continue
        results.append(read_result(path))
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',type=Path,required=True);parser.add_argument('--response',type=Path,required=True)
    args=parser.parse_args()
    request=json.loads(args.request.read_text())
    try:
        mode=request['operation']
        if mode=='discover': result={'sequences':[discover(p) for p in request['paths']]}
        elif mode=='budget': result=batch_plan(request['configs'],request.get('workers',0))
        elif mode=='open': result={'results':open_results(request['paths'])}
        else: raise ValueError('Unknown trajectory operation')
    except Exception as error:
        result={'error':str(error)}
    args.response.write_text(json.dumps(result,allow_nan=False)+'\n',encoding='utf-8')


if __name__=='__main__':main()
