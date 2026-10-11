"""Names and independent review locations for published registration results."""

import json
import pathlib
import re


def mapBase(_path):
    return re.sub(r"_[0-9]+(?:\.[0-9]+)?cm$", "", _path.stem)


def posesPath(_map_path):
    return _map_path.with_name(mapBase(_map_path) + "_relative_poses.json")


def reviewDirectory(_directory,
                    _name):
    return _directory


def reviewForRecords(_records):
    config_path = _records / "config.json"
    if config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if config.get("review_directory"):
            return pathlib.Path(config["review_directory"])
        if config.get("output_path"):
            target = pathlib.Path(config["output_path"])
            return reviewDirectory(target.parent, mapBase(target))
    name = _records.name.removesuffix(".registration")
    return reviewDirectory(_records.parent, name)
