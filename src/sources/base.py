from pathlib import Path
import hashlib
import os
import json
import datetime
import traceback
import unicodedata
import re
import time
from copy import deepcopy
from typing import Tuple, List, Dict, Type, Optional, Union, Generator, Iterable

import requests
import bs4


installed_sources: Dict[str, Type["SourceBase"]] = dict()


_re_double_minus = re.compile(r"--+")


class ScraperError(Exception):
    def __init__(self, *args, data: Optional[dict] = None):
        super().__init__(*args)
        if not data:
            data = dict()
        data["stacktrace"] = traceback.format_exc(limit=3)
        self.data: dict = data


class SourceBase:

    CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "cache"
    SNAPSHOT_DIR = Path(__file__).resolve().parent.parent.parent / "snapshots"
    ERROR_DIR = Path(__file__).resolve().parent.parent.parent / "errors"

    SCRAPER_TYPE = "custom"
    VERIFY_CERTIFICATE = True
    REQUEST_DELAY = 0
    # provide a name to exclude from parallel processing
    MULTI_PROCESS_GROUP: Optional[str] = None

    ID: str = None
    NAME: str = None
    BASE_URL: str = None

    def __init_subclass__(cls, **kwargs):
        if "Base" not in cls.__name__:
            assert cls.ID, f"Must define {cls.__name__}.ID"
            assert cls.NAME, f"Must define {cls.__name__}.NAME"
            if cls.ID in installed_sources:
                raise AssertionError(
                    f"Duplicate scraper ID '{cls.ID}' used in {installed_sources[cls.ID].__name__} and {cls.__name__}"
                )
            installed_sources[cls.ID] = cls

    def __init__(self, num_weeks: int = 4, use_cache: bool = False):
        self.session = requests.Session()
        self.use_cache = use_cache
        self.num_weeks = num_weeks

        self.new_session()

    def make_snapshot(self):
        raise NotImplementedError

    @classmethod
    def index_url(cls) -> str:
        return cls.BASE_URL

    @classmethod
    def convert_snapshot(cls, data: Union[dict, list]) -> List[dict]:
        raise NotImplementedError

    @classmethod
    def compare_snapshot_location(
            cls,
            prev_timestamp: datetime.datetime, prev_data: dict,
            timestamp: datetime.datetime, data: dict,
            working_data: dict,
    ) -> Tuple[set, set, bool]:
        appointments, cancellations = set(), set()

        #df = get_calendar_table(data["dates"])
        #print(df)

        sorted_dates = sorted(data["dates"])
        for d in prev_data["dates"]:
            if d not in data["dates"] and sorted_dates[0] <= d <= sorted_dates[-1]:
                appointments.add(d)

        sorted_prev_dates = sorted(prev_data["dates"])
        for d in data["dates"]:
            if d not in prev_data["dates"] and sorted_prev_dates[0] <= d <= sorted_prev_dates[-1]:
                cancellations.add(d)

        if sorted_dates == sorted_prev_dates:
            changed = False
        else:
            sorted_dates = [d for d in sorted_dates if d >= prev_timestamp]
            changed = sorted_dates != sorted_prev_dates
        return appointments, cancellations, changed

    def iter_snapshot_filenames(
            self,
            date_from: Optional[str] = None,
            date_to: Optional[str] = None,
    ) -> Generator[Tuple[datetime.datetime, Path], None, None]:
        for fn in sorted((self.SNAPSHOT_DIR / self.ID).glob("*/*.json")):
            date_str = fn.name[:19]
            if date_from and not date_str >= date_from:
                continue
            if date_to and not date_str < date_to:
                continue
            dt = datetime.datetime.strptime(date_str, "%Y-%m-%d-%H-%M-%S")
            yield dt, fn

    def iter_error_filenames(
            self,
            date_from: Optional[str] = None,
            date_to: Optional[str] = None,
    ) -> Generator[Tuple[datetime.datetime, Path], None, None]:
        for fn in (self.ERROR_DIR / self.ID).glob("*/*.json"):
            date_str = fn.name[:19]
            if date_from and not date_str >= date_from:
                continue
            if date_to and not date_str < date_to:
                continue
            dt = datetime.datetime.strptime(date_str, "%Y-%m-%d-%H-%M-%S")
            yield dt, fn

    def iter_snapshot_data(
            self,
            date_from: Optional[str] = None,
            date_to: Optional[str] = None,
            with_unchanged: bool = False
    ) -> Generator[Tuple[datetime.datetime, bool, Union[dict, list]], None, None]:
        previous_data = None
        for fn in sorted((self.SNAPSHOT_DIR / self.ID).glob("*/*.json")):
            date_str = fn.name[:19]
            dt = datetime.datetime.strptime(date_str, "%Y-%m-%d-%H-%M-%S")
            unchanged = "unchanged.json" in str(fn)
            if not unchanged:
                data = json.loads(fn.read_text())
                if not (isinstance(data, dict) and "unchanged" in data):
                    yield dt, False, data
                    previous_data = data
                    continue

            date_str = fn.name[:19]
            if date_from and not date_str >= date_from:
                continue
            if date_to and not date_str < date_to:
                continue

            # yield unchanged data
            if with_unchanged:
                assert previous_data is not None, f"unchanged data before real data @ {dt} / {fn}"
                yield dt, True, previous_data

    # ---- below are all helpers for derived classes ----

    def now(self) -> datetime.datetime:
        return datetime.datetime.now()

    def new_session(self):
        if self.session:
            self.session.close()
        self.session = requests.Session()
        self.session.headers = {
            "User-Agent": "Mozilla/5.0 Gecko/20100101 Firefox/84.0"
        }

    @classmethod
    def to_id(cls, name: str) -> str:
        name = str(name).lower()
        name = name.replace("ß", "ss")
        name = unicodedata.normalize('NFKD', name).encode("ascii", "ignore").decode("ascii")

        name = "".join(
            c if c.isalnum() or c in " \t" else "-"
            for c in name
        ).replace(" ", "-")

        return _re_double_minus.sub("-", name).strip("-")

    def get_cache_dir(self) -> Path:
        return self.CACHE_DIR / self.ID

    def get_cache_filename(
            self,
            x,
            data: Optional[Union[str, dict]] = None,
            headers: Optional[dict] = None
    ) -> Path:
        x = str(x)
        if data:
            if isinstance(data, str):
                x += data
            else:
                x += json.dumps(data)
        if headers:
            x += json.dumps(headers)
        hash = hashlib.md5(x.encode("utf-8")).hexdigest()
        return self.get_cache_dir() / hash

    def get_url(self, url, method="GET", data=None, headers: Optional[dict] = None, encoding=None) -> str:
        if self.use_cache:
            cache_filename = self.get_cache_filename(url, data, headers)
            if cache_filename.exists():
                with open(str(cache_filename)) as fp:
                    return fp.read()

        for try_num in range(3):
            try:
                print("downloading", url, data or "")
                kwargs = dict(
                    timeout=10,
                    verify=self.VERIFY_CERTIFICATE,
                    headers=headers,
                )
                if data:
                    if method == "GET":
                        kwargs["params"] = data
                    else:
                        kwargs["data"] = data
                if self.REQUEST_DELAY:
                    time.sleep(self.REQUEST_DELAY)
                response = self.session.request(method, url, **kwargs)
                if encoding is None:
                    text = response.text
                else:
                    text = response.content.decode(encoding)
                break
            except requests.ConnectionError:
                if try_num == 2:
                    raise

        if self.use_cache:
            os.makedirs(str(self.get_cache_dir()), exist_ok=True)
            with open(str(cache_filename), "w") as fp:
                fp.write(text)

        return text

    def get_json(self, url, method="GET", data=None, headers=None, encoding=None) -> dict:
        text = self.get_url(url, method, data=data, encoding=encoding, headers=headers)
        try:
            return json.loads(text)
        except Exception as e:
            print(text[:1000])
            raise ScraperError(
                f"{type(e).__name__}: {e}", data={
                    "text": self._get_html_error_text(text)
                }
            )

    def _get_html_error_text(self, text: str) -> str:
        if "<title>" in text:
            msg = text[text.index("<title>")+7:]
            if "</title>" in msg:
                return msg[:msg.index("</title>")]
        else:
            msg = text
        return msg[:10000]

    def get_html_soup(self, url, method="GET", data=None, headers: Optional[dict] = None, encoding=None):
        text = self.get_url(url, method=method, data=data, headers=headers, encoding=encoding)
        soup = self.soup(text)
        return soup

    def soup(self, html: str):
        return bs4.BeautifulSoup(html, features="html.parser")


def get_calendar_table(dates: List[str]):
    import numpy as np
    import pandas as pd
    dates_dic = {}
    times = set()
    for d in sorted(dates):
        if isinstance(d, datetime.datetime):
            d = d.isoformat()
        date = d[:10]
        time = d[11:16]
        if date not in dates_dic:
            dates_dic[date] = {}
        dates_dic[date][time] = 1
        times.add(time)

    df = (
        pd.DataFrame(dates_dic)
        .rename({0: "date"})
        .replace(np.nan, 0)
        #.set_index(0)
        .sort_index()
    )
    return df
