"""Back up and convert absolute paths in environment/output JSON files.

Dry run by default. Use --apply after reviewing the proposed changes.
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import zipfile


def fileDigest(path):
    result = hashlib.sha256()
    before = path.stat()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b''):
            result.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f'Scan changed while checking relocation: {path}')
    return result.hexdigest()


def absolute(value):
    return isinstance(value, str) and (Path(value).is_absolute() or PureWindowsPath(value).is_absolute())


def prepare(root):
    originals, updates, changes = {}, {}, []
    for output in sorted(root.glob("*/output")):
        index = {}
        for path in output.rglob("*"):
            if path.is_file():
                index.setdefault(path.name, []).append(path)
        for file in sorted(output.rglob("*.json")):
            original = file.read_bytes()
            originals[file] = original

            def convert(value):
                normalized = value.replace("\\", "/")
                anchor = "/" + output.relative_to(root).as_posix() + "/"
                path = Path(value) if absolute(value) else file.parent / value
                local = output / normalized.rsplit(anchor, 1)[1] if anchor in normalized else None
                relocate = local is not None and local.is_file() and path.resolve() != local.resolve()
                if not absolute(value) and not relocate:
                    return value
                content_hash = None
                if relocate:
                    # A mounted old copy can still exist. Prefer the copy in
                    # this environment, but verify its complete contents first.
                    if path.is_file() and not path.samefile(local):
                        if path.stat().st_size != local.stat().st_size:
                            raise ValueError(f'Local copy differs from original: {local}')
                        content_hash = fileDigest(local)
                        if fileDigest(path) != content_hash:
                            raise ValueError(f'Local copy differs from original: {local}')
                    path = local
                elif not path.exists():
                    if anchor in normalized:
                        # Keep old entries for absent scans tied to their original
                        # environment; never borrow a namesake from another one.
                        path = output / normalized.rsplit(anchor, 1)[1]
                    else:
                        candidates = index.get(normalized.rsplit("/", 1)[-1], [])
                        if len(candidates) != 1:
                            raise ValueError(f"Cannot uniquely relocate {value} in {file}")
                        path = candidates[0]
                path = path.resolve()
                relative = Path(os.path.relpath(path, file.parent)).as_posix()
                assert (file.parent / relative).resolve() == path
                changes.append(dict(json=str(file.relative_to(root)), before=value, after=relative,
                                    resolved=str(path), exists=path.exists(), verified_content_sha256=content_hash))
                return relative

            def visit(value):
                if isinstance(value, dict):
                    result = {}
                    for key, item in value.items():
                        new_key = convert(key)
                        if new_key in result:
                            raise ValueError(f"Path key collision in {file}: {new_key}")
                        result[new_key] = visit(item)
                    return result
                if isinstance(value, list):
                    return [visit(item) for item in value]
                return convert(value) if isinstance(value, str) else value

            document = json.loads(original)
            updated = visit(document)
            if updated != document:
                updates[file] = (json.dumps(updated, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    # A path-only manifest rewrite changes its digest. Preserve product bindings.
    hashes = {hashlib.sha256(originals[p]).hexdigest(): hashlib.sha256(b).hexdigest()
              for p, b in updates.items() if p.name == "relative_poses.json"}
    for file, original in originals.items():
        if file.name != "products.json" or not hashes:
            continue
        document = json.loads(updates.get(file, original))
        changed = False
        for product in document.values():
            old = product.get("poses_sha256")
            if old in hashes:
                product["poses_sha256"] = hashes[old]
                changed = True
        if changed:
            updates[file] = (json.dumps(document, indent=2) + "\n").encode()
    return originals, updates, changes


def migrate(root, apply=False):
    root = Path(root).resolve(strict=True)
    originals, updates, changes = prepare(root)
    report = dict(root=str(root), json_files=len(originals), changed_files=len(updates),
                  converted_paths=len(changes), changes=changes)
    if not apply or not updates:
        return report
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = root / "json_path_backups" / stamp
    backup.mkdir(parents=True)
    archive = backup / "original_json.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as zipped:
        for file, data in originals.items():
            zipped.writestr(file.relative_to(root).as_posix(), data)
    with zipfile.ZipFile(archive) as zipped:
        for file, data in originals.items():
            if zipped.read(file.relative_to(root).as_posix()) != data:
                raise ValueError(f"Backup verification failed: {file}")
    # Recheck every input before the first replacement.
    for file, data in originals.items():
        if file.read_bytes() != data:
            raise ValueError(f"JSON changed during migration: {file}")
    report["backup"] = str(archive)
    report["files"] = [{"path": str(p.relative_to(root)),
                        "before_sha256": hashlib.sha256(originals[p]).hexdigest(),
                        "after_sha256": hashlib.sha256(data).hexdigest()} for p, data in updates.items()]
    (backup / "migration.json").write_text(json.dumps(report, indent=2) + "\n")
    for file, data in updates.items():
        pending = file.with_name(file.name + ".relative-paths.pending")
        with pending.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        pending.chmod(file.stat().st_mode)
        pending.replace(file)
        if file.read_bytes() != data:
            raise ValueError(f"Write verification failed: {file}")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = migrate(args.root, args.apply)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key not in ("changes", "files")}, indent=2))
