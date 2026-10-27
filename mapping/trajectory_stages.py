"""Durable completion receipts for staged trajectory campaigns."""
import json
from pathlib import Path

from trajectory_io import digest, save_json


def baseline_files(output):
    root = Path(output)/'baseline'
    names = ['candidate_trajectory.tum', 'quality.jsonl', 'initial_alignment.json', 'input_quality.json']
    if (root/'forward_attempts.jsonl').exists(): names.append('forward_attempts.jsonl')
    return {name:digest(root/name) for name in names}


def mark_baseline_ready(output, selected=None):
    output = Path(output)
    rows = [json.loads(line) for line in (output/'baseline/quality.jsonl').read_text().splitlines()]
    result = dict(status='baseline_ready', processed=len(rows), accepted=sum(bool(q['accepted']) for q in rows),
                  selected=selected, files=baseline_files(output))
    save_json(output/'baseline/status.json', result)
    return result


def ready_baseline(output, *, adopt_legacy=False, selected=None):
    output = Path(output)
    receipt = output/'baseline/status.json'
    if receipt.exists():
        result = json.loads(receipt.read_text())
        if result.get('status') != 'baseline_ready' or result.get('files') != baseline_files(output):
            raise ValueError('Completed 1 cm baseline changed; preserve it and use a new result folder')
        return result
    # Recovery can only have started after the complete baseline was published.
    legacy = output/'recovery/provenance.json'
    if adopt_legacy and legacy.exists():
        proof = json.loads(legacy.read_text())
        if proof.get('baseline_policy') == 'verified-1cm':
            if (proof.get('baseline_trajectory_sha256') != digest(output/'baseline/candidate_trajectory.tum')
                    or proof.get('baseline_quality_sha256') != digest(output/'baseline/quality.jsonl')):
                raise ValueError('Completed 1 cm baseline changed since the previous recovery')
            return mark_baseline_ready(output, selected)
    return None
