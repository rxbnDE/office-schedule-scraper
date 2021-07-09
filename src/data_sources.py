import fnmatch
import json
import datetime
import os
import traceback
from multiprocessing.pool import Pool
from pathlib import Path
from typing import Generator, Optional, List, Type

from .sources.base import installed_sources, SourceBase


class DataSources:

    SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "snapshots"
    ERROR_DIR = Path(__file__).resolve().parent.parent / "errors"

    def __init__(
            self,
            use_cache: bool = False,
            include: Optional[str] = None,
            exclude: Optional[str] = None,
    ):
        self.use_cache = use_cache
        self.include = include
        self.exclude = exclude

        self.source_classes: List[Type[SourceBase]] = []
        for name, cls in installed_sources.items():
            if exclude and fnmatch.fnmatchcase(name, exclude):
                continue
            if include and not fnmatch.fnmatchcase(name, include):
                continue

            self.source_classes.append(cls)

        self.source_classes.sort(key=lambda s: s.ID)

    def dump_list(self):
        for i, s in enumerate(self.source_classes):
            print(f"{i+1}. {s.ID}")

    def sources(self, num_weeks: int = 4):
        return [
            c(use_cache=self.use_cache, num_weeks=num_weeks)
            for c in self.source_classes
        ]

    def dump_snapshot(self, num_weeks: int = 4):
        for s in self.sources(num_weeks=num_weeks):
            data = s.make_snapshot()
            print(f"---------- {s.ID} ----------")
            print(json.dumps(data, indent=2))

    def make_snapshot(self, num_weeks: int = 4, processes: int = 1):
        sources = self.sources(num_weeks=num_weeks)

        if processes <= 1:
            for s in sources:
                self._make_and_store_snapshot(s)
        else:
            pool = Pool(processes)
            pool.map(self._make_and_store_snapshot, sources)

    def _make_and_store_snapshot(self, s: SourceBase):
        error = False
        try:
            data = s.make_snapshot()
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            data = {
                "class": type(e).__name__,
                "message": str(e),
                "stacktrace": traceback.format_exc(limit=3),
            }
            error = True

        self._store_snapshot(s, data, error)

    def _store_snapshot(self, s: SourceBase, data: dict, error: bool = False):
        now = datetime.datetime.now()

        snapshot_dir = (self.SNAPSHOT_DIR if not error else self.ERROR_DIR) / s.ID / now.strftime("%Y-%m")
        os.makedirs(snapshot_dir, exist_ok=True)

        # save some disk space
        filename = snapshot_dir / now.strftime("%Y-%m-%d-%H-%M-%S.json")
        if self._is_same_data(data, snapshot_dir):
            print(f"{s.ID}: unchanged")
            data = {}
            filename = snapshot_dir / now.strftime("%Y-%m-%d-%H-%M-%S-unchanged.json")

        print(f"writing {filename}")
        with open(filename, "w") as fp:
            json.dump(data, fp, cls=JsonEncoder)

    def _is_same_data(self, data: dict, path: Path) -> bool:
        files = sorted(filter(
            lambda n: "unchanged" not in str(n),
            path.glob("*.json")
        ))
        if not files:
            return False

        file = files[-1]
        with open(str(file)) as fp:
            return data == json.load(fp)


class JsonEncoder(json.JSONEncoder):

    def default(self, o):
        try:
            return super().default(o)
        except Exception:
            print("X", type(o))
            raise