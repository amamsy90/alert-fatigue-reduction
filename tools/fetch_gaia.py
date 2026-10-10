"""Download the GAIA MicroSS files this project uses and check their SHA-256.

GAIA (CloudWise, 2021) is the main benchmark: two months of a microservice
system with 1,367 injected memory faults. Only two parts are used here:

  run tables     MicroSS/run/run.zip            -> app/data/gaia_raw/run/run/run_table_2021-0{7,8}.csv
  memory series  MicroSS/metric/metric_split.*  -> app/data/gaia_metrics/metric/*_docker_memory_usage_total_*.csv

The metric archive is a 35-part split zip (about 1.8 GB). It is downloaded to
a cache folder, and only the 37 docker_memory_usage_total files are extracted.
No external unzip tool is needed. Every extracted file is checked against the
SHA-256 of the copy used for the published results; a mismatch stops the run.

    python tools/fetch_gaia.py              # run tables + memory series
    python tools/fetch_gaia.py --run-only   # run tables only (under 1 MB)
    python tools/fetch_gaia.py --check      # only verify files already in place
    python tools/fetch_gaia.py --keep-cache # keep the 1.8 GB split archive

Source: https://github.com/CloudWise-OpenSource/GAIA-DataSet (MicroSS).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import struct
import sys
import zipfile
import zlib
from pathlib import Path
from urllib.request import urlopen

RAW = "https://raw.githubusercontent.com/CloudWise-OpenSource/GAIA-DataSet/main/MicroSS"
DATA = Path(__file__).resolve().parents[1] / "app" / "data"
CACHE = DATA / "gaia_download"
METRIC_PARTS = [f"metric_split.z{i:02d}" for i in range(1, 35)] + ["metric_split.zip"]
MEMORY_MARK = "docker_memory_usage_total"

RUN_ZIP = ("4ba62f1fa4033b1394830d23d9fee42b0ee3e5d00918e91dc6feed7c3035357c", 823135)
# Path under app/data -> (SHA-256, size in bytes) of the files used for the results.
EXPECTED: dict[str, tuple[str, int]] = {
    "gaia_raw/run/run/run_table_2021-07.csv": ("de56f41fa0f21ed22b084558d0a5149c627de46fd82f18a26903d1b4424166a1", 3643229),
    "gaia_raw/run/run/run_table_2021-08.csv": ("4bcc6b8d4fc5496648d6f71da2985ebd94428b24c1e71d38866da3f6d1528ace", 4181143),
    "gaia_metrics/metric/dbservice1_0.0.0.4_docker_memory_usage_total_2021-07-01_2021-07-15.csv": ("31f2749c096a175d1e1ac748afe2b71b94d7620f36a9eded1d2363cbc38e7081", 941434),
    "gaia_metrics/metric/dbservice1_0.0.0.4_docker_memory_usage_total_2021-07-15_2021-07-31.csv": ("4d5fb9af5b358cfb483ab92775516fe68059eadc24457632809dfa48cebd3687", 1118448),
    "gaia_metrics/metric/dbservice1_0.0.0.4_docker_memory_usage_total_2021-08-01_2021-08-31.csv": ("fed76eba7305aee40a63ffdaffbb3f620a791ffae5d21b53f6f4344144773e44", 1840295),
    "gaia_metrics/metric/dbservice2_0.0.0.2_docker_memory_usage_total_2021-07-01_2021-07-15.csv": ("db502d134451017a0f8ca52f4ae829b9864a414f3b70662281fcc7be80a8b580", 834286),
    "gaia_metrics/metric/dbservice2_0.0.0.2_docker_memory_usage_total_2021-07-15_2021-07-31.csv": ("89b4542d6a39c34b67216097c4ee68de75aa26495c1757da7f03ead25ea0e62f", 1145086),
    "gaia_metrics/metric/dbservice2_0.0.0.2_docker_memory_usage_total_2021-08-01_2021-08-31.csv": ("cfdcbc3c8d5da0d317be2ad9f6c59b666bb48302b1723b6d4612f26ea5af6a19", 1883761),
    "gaia_metrics/metric/logservice1_0.0.0.3_docker_memory_usage_total_2021-07-01_2021-07-15.csv": ("ec3952289a0154adf816a3025c20ee4c3f6e061668e0b91c6ab43e7cc06e899c", 875059),
    "gaia_metrics/metric/logservice1_0.0.0.3_docker_memory_usage_total_2021-07-15_2021-07-31.csv": ("75167930d4fd13b628d6a747fbe15ff6e73f3cb0b212da3b10fe4cc086cbf00c", 1142368),
    "gaia_metrics/metric/logservice1_0.0.0.3_docker_memory_usage_total_2021-08-01_2021-08-31.csv": ("760a80c16d904bbf222bfe9d250f6f3f13de59dc602615d2949cf62b3cbc59e4", 1876931),
    "gaia_metrics/metric/logservice2_0.0.0.2_docker_memory_usage_total_2021-07-01_2021-07-15.csv": ("0833b25ae1a1aaa0c185ce616706366a2c1f42ab8d62fd578ee015abd2570b12", 834215),
    "gaia_metrics/metric/logservice2_0.0.0.2_docker_memory_usage_total_2021-07-15_2021-07-31.csv": ("7e8e934c9c887fdc5959422e0c92c7be6208c23ec5d73ab22d13943239574c6b", 1145012),
    "gaia_metrics/metric/logservice2_0.0.0.2_docker_memory_usage_total_2021-08-01_2021-08-31.csv": ("a242125785b3f67a36afa1af28caa4bf5a701063df1da0b20f8f96dcef6fca06", 1885675),
    "gaia_metrics/metric/mobservice1_0.0.0.1_docker_memory_usage_total_2021-07-01_2021-07-15.csv": ("877b9719e6043e4155327fdd60cbc7e169fd4b17ff14284a54876c006b50bb29", 856247),
    "gaia_metrics/metric/mobservice1_0.0.0.1_docker_memory_usage_total_2021-07-15_2021-07-31.csv": ("43db10a0baefb660dfca7f9a8aba0f643dca4c98e871c373bab6d9b6ed757524", 1143817),
    "gaia_metrics/metric/mobservice1_0.0.0.1_docker_memory_usage_total_2021-08-01_2021-08-31.csv": ("a6857549860d2b98140eca83f5a053879eeaa6a823549dcdd958166c9942090a", 1873356),
    "gaia_metrics/metric/mobservice2_0.0.0.4_docker_memory_usage_total_2021-07-01_2021-07-15.csv": ("216e0640fc028fdadcc5edd6436775edb67e12809dda4114b0b94ec5c68575af", 935191),
    "gaia_metrics/metric/mobservice2_0.0.0.4_docker_memory_usage_total_2021-07-15_2021-07-31.csv": ("a50f2a8330af3c8a7054328f2c516fb9ac61c16d42429466734e8d322384fc58", 1108751),
    "gaia_metrics/metric/mobservice2_0.0.0.4_docker_memory_usage_total_2021-08-01_2021-08-31.csv": ("05cc8140d4f2d9e09265963d74356efe85b478eeaf60a49d41eee0c93c9c36c3", 1837969),
    "gaia_metrics/metric/redisservice1_0.0.0.1_docker_memory_usage_total_2021-07-01_2021-07-15.csv": ("d85c37f7ba65ea17be1b24dc80bcffa5ce41f647d9e489e97e6f2457ae5a225c", 856267),
    "gaia_metrics/metric/redisservice1_0.0.0.1_docker_memory_usage_total_2021-07-15_2021-07-31.csv": ("d5c841ecf87e90a1016bfeb8452ca96a408b35295c3a7d5bae5b5b41e8bcc0ec", 1143979),
    "gaia_metrics/metric/redisservice1_0.0.0.1_docker_memory_usage_total_2021-08-01_2021-08-31.csv": ("00770c0d33b5cada07bed6b0cbd58e112a0ab8fb2b805885759da19b893d420c", 1873616),
    "gaia_metrics/metric/redisservice2_0.0.0.2_docker_memory_usage_total_2021-07-01_2021-07-15.csv": ("5edaa87e9ea98afccdb81d122f2f43c17c7cf7731f90c00b86f19b22c50a6eac", 834432),
    "gaia_metrics/metric/redisservice2_0.0.0.2_docker_memory_usage_total_2021-07-15_2021-07-31.csv": ("456bfd83ae05f0efadec1c4aaede68a8fdd2d8e32722fac5ea8ef1d0baa38a70", 1145086),
    "gaia_metrics/metric/redisservice2_0.0.0.2_docker_memory_usage_total_2021-08-01_2021-08-31.csv": ("f4a0c90fe3a330db075be66053ee5044e05f53a1022adb8df35b4aa15bdc0203", 1887338),
    "gaia_metrics/metric/redis_0.0.0.3_docker_memory_usage_total_2021-07-01_2021-07-15.csv": ("2a7952483f449e1820728d794e2e9102842e8ab461d98bdb51a6ba34fda5bf2b", 810216),
    "gaia_metrics/metric/redis_0.0.0.3_docker_memory_usage_total_2021-07-15_2021-07-31.csv": ("b471742d36bdc89faca2363a1f4282658dd15a0d9783b03a33ff9ec9da97542e", 1057941),
    "gaia_metrics/metric/redis_0.0.0.3_docker_memory_usage_total_2021-08-01_2021-08-31.csv": ("cc8fc68eff9bc7b83ce208bf3949a1772e341794f0fb6ed2bffe1e2dca160925", 1729340),
    "gaia_metrics/metric/webservice1_0.0.0.1_docker_memory_usage_total_2021-07-01_2021-07-15.csv": ("811c38540ebcae2ed52b04da810f3c9a74978285aca7ace6fca6e3020694c15d", 854443),
    "gaia_metrics/metric/webservice1_0.0.0.1_docker_memory_usage_total_2021-07-15_2021-07-31.csv": ("2f79b12db22fcf866332b6db6237a4393231e599083f632e98196b4c4340a531", 1143804),
    "gaia_metrics/metric/webservice1_0.0.0.1_docker_memory_usage_total_2021-08-01_2021-08-31.csv": ("be9ad4bd95e59a6695d621c1500348a8c7bb92e0b82a7bd565185fd75ea140d5", 1873700),
    "gaia_metrics/metric/webservice2_0.0.0.3_docker_memory_usage_total_2021-07-01_2021-07-15.csv": ("f1a445d0b999e49fb3bdfe27f9029950fcbeadb1d39ad02923515989228813fa", 873181),
    "gaia_metrics/metric/webservice2_0.0.0.3_docker_memory_usage_total_2021-07-15_2021-07-31.csv": ("bbdd0e8bc1c358ab8338140667a7465e25b150c00fa05ea04628fec8f4b49f0a", 1143017),
    "gaia_metrics/metric/webservice2_0.0.0.3_docker_memory_usage_total_2021-08-01_2021-08-31.csv": ("7411df51f3196bb0c4e37d5ff1971dff39f034b49ad1f7728b68b46670605cdb", 1875295),
    "gaia_metrics/metric/zookeeper_0.0.0.1_docker_memory_usage_total_2021-07-01_2021-07-15.csv": ("a72b0675a9e8561580c4910d80dabf3c816c6cc2a562ea8d3b37e1c39e142fbb", 824606),
    "gaia_metrics/metric/zookeeper_0.0.0.1_docker_memory_usage_total_2021-07-15_2021-07-31.csv": ("bfd07fd6505c2928eba938b9a5e27f3155b469f18b3bc0e2010edbbe8da9bd7b", 1101324),
    "gaia_metrics/metric/zookeeper_0.0.0.1_docker_memory_usage_total_2021-08-01_2021-08-01.csv": ("6c9551a982107ed6f9f00de690389dbc52bbb35f7171b7dafb43faa3a9a7b321", 40),
    "gaia_metrics/metric/zookeeper_0.0.0.1_docker_memory_usage_total_2021-08-01_2021-08-31.csv": ("13b6c5ec78a52c5c7b7eefe8eebd39b73c8ffa146f1ee9e18fe20ad147c48b7e", 1775235),
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(url: str, dest: Path) -> Path:
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  downloading {url}")
    with urlopen(url, timeout=300) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    tmp.replace(dest)
    return dest


def write_checked(rel: str, data: bytes) -> None:
    want_hash, want_size = EXPECTED[rel]
    if len(data) != want_size or sha256(data) != want_hash:
        raise SystemExit(f"SHA-256 mismatch for {rel}: the upstream file changed; results may differ")
    target = DATA / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def fetch_run() -> None:
    blob = download(f"{RAW}/run/run.zip", DATA / "gaia_raw" / "run.zip").read_bytes()
    if (sha256(blob), len(blob)) != RUN_ZIP:
        raise SystemExit("SHA-256 mismatch for run.zip: the upstream file changed")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for name in archive.namelist():
            if name.endswith(".csv"):
                write_checked(f"gaia_raw/run/{name}", archive.read(name))
    print("  run tables OK")


def _central_directory(last: bytes) -> tuple[int, int, int]:
    """(disk of the central directory, its offset on that disk, number of
    entries) from the end of the last part, following a zip64 record if any."""
    pos = last.rfind(b"PK\x05\x06")
    if pos < 0:
        raise SystemExit("metric_split.zip has no end-of-central-directory record")
    _this_disk, cd_disk, _here, total, _size, cd_offset, _clen = struct.unpack("<HHHHIIH", last[pos + 4:pos + 22])
    rec = last.rfind(b"PK\x06\x06", 0, pos)
    if rec >= 0 and (cd_offset == 0xFFFFFFFF or total == 0xFFFF or cd_disk == 0xFFFF):
        fields = struct.unpack("<QHHIIQQQQ", last[rec + 4:rec + 56])
        cd_disk, total, cd_offset = fields[4], fields[6], fields[8]
    return cd_disk, cd_offset, total


def fetch_metrics(keep_cache: bool) -> None:
    parts = [download(f"{RAW}/metric/{name}", CACHE / name) for name in METRIC_PARTS]
    starts, offset = [], 0
    for p in parts:
        starts.append(offset)
        offset += p.stat().st_size
    joined = CACHE / "metric_joined.bin"
    if not joined.exists() or joined.stat().st_size != offset:
        with open(joined, "wb") as out:
            for p in parts:
                with open(p, "rb") as src:
                    shutil.copyfileobj(src, out, 1 << 20)
    last = parts[-1].read_bytes()
    cd_disk, cd_offset, total = _central_directory(last)
    with open(joined, "rb") as f:
        f.seek(starts[cd_disk] + cd_offset)
        found = 0
        records = []
        for _ in range(total):
            head = f.read(46)
            if head[:4] != b"PK\x01\x02":
                raise SystemExit("unexpected central-directory layout")
            (method, comp, size, n_len, x_len, c_len, disk, local) = (
                struct.unpack("<H", head[10:12])[0],
                *struct.unpack("<II", head[20:28]),
                *struct.unpack("<HHHH", head[28:36]),
                struct.unpack("<I", head[42:46])[0],
            )
            name = f.read(n_len).decode("utf-8", "replace")
            extra = f.read(x_len)
            f.read(c_len)
            # zip64 extra field: sizes, offset and disk replace 0xFFFF... markers in that order.
            i = 0
            while i + 4 <= len(extra):
                tag, ln = struct.unpack("<HH", extra[i:i + 4])
                body = extra[i + 4:i + 4 + ln]
                if tag == 1:
                    j = 0
                    if size == 0xFFFFFFFF:
                        size = struct.unpack("<Q", body[j:j + 8])[0]
                        j += 8
                    if comp == 0xFFFFFFFF:
                        comp = struct.unpack("<Q", body[j:j + 8])[0]
                        j += 8
                    if local == 0xFFFFFFFF:
                        local = struct.unpack("<Q", body[j:j + 8])[0]
                        j += 8
                    if disk == 0xFFFF:
                        disk = struct.unpack("<I", body[j:j + 4])[0]
                i += 4 + ln
            if MEMORY_MARK in name and name.endswith(".csv"):
                records.append((name, method, comp, size, starts[disk] + local))
        for name, method, comp, size, at in records:
            f.seek(at)
            head = f.read(30)
            if head[:4] != b"PK\x03\x04":
                raise SystemExit(f"bad local header for {name}")
            n_len, x_len = struct.unpack("<HH", head[26:30])
            f.seek(at + 30 + n_len + x_len)
            raw = f.read(comp)
            data = zlib.decompress(raw, -15) if method == 8 else raw
            if len(data) != size:
                raise SystemExit(f"{name}: extracted {len(data)} bytes, expected {size}")
            rel = "gaia_metrics/metric/" + Path(name).name
            if rel not in EXPECTED:
                print(f"  skipping {name} (not used by the published results)")
                continue
            write_checked(rel, data)
            found += 1
    missing = [r for r in EXPECTED if r.startswith("gaia_metrics/") and not (DATA / r).exists()]
    if missing:
        raise SystemExit(f"{len(missing)} expected memory files were not in the archive: {missing[:3]}")
    print(f"  memory series OK ({found} files)")
    if not keep_cache:
        shutil.rmtree(CACHE, ignore_errors=True)


def check() -> int:
    bad = 0
    for rel, (want, size) in EXPECTED.items():
        path = DATA / rel
        if not path.exists():
            print(f"missing   {rel}")
            bad += 1
        elif path.stat().st_size != size or sha256(path.read_bytes()) != want:
            print(f"MISMATCH  {rel}")
            bad += 1
    print("GAIA files OK" if not bad else f"{bad} GAIA file(s) missing or different")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-only", action="store_true", help="only the run tables")
    ap.add_argument("--check", action="store_true", help="only verify files already in app/data")
    ap.add_argument("--keep-cache", action="store_true", help="keep the downloaded split archive")
    args = ap.parse_args()
    if args.check:
        sys.exit(check())
    fetch_run()
    if not args.run_only:
        fetch_metrics(args.keep_cache)
    sys.exit(check() if not args.run_only else 0)
