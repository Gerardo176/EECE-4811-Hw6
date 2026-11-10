"""
benchmark.py - performance & storage-efficiency experiments for raid.py

Usage:
    python3 benchmark.py                 # all experiments (~ tens of minutes)
    python3 benchmark.py --quick         # small smoke run
    python3 benchmark.py --only E1 E4    # subset

Raw results -> results/results.json, summary tables -> results/summary.md

Measurement methodology (identical for every RAID level):
  * Every level stores the SAME logical data set; disk files are sized from
    the level's storage efficiency.
  * Before a timed write phase the array is pre-filled with other data,
    fsync'ed and evicted from the page cache, so writes are true overwrites
    and RAID 4/5 read-modify-write has to fetch old data/parity from storage.
  * Sync policy "end": the write phase ends with fsync() on all five disks
    and that fsync is included in the timing.  Sync policy "each": every
    physical block write is fsync'ed before the request completes.
  * Before the read phase, all disk files are fsync'ed and evicted from the
    page cache (posix_fadvise DONTNEED), so reads hit the storage device.
  * Every block read is compared against the data that was written.
"""

import argparse
import json
import os
import random
import shutil
import statistics
import sys
import threading
import time

from raid import LEVELS, NUM_DISKS, make_raid

MB = 1024 * 1024
HERE = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(HERE, "disks")
OUTDIR = os.path.join(HERE, "results")

_buffers = {}


def payload(total_bytes, seed):
    """Random payload (cached) - generated once so data generation is never timed."""
    key = (total_bytes, seed)
    if key not in _buffers:
        for k in [k for k in _buffers if k[0] != total_bytes]:
            del _buffers[k]          # keep only buffers of the current size
        _buffers[key] = memoryview(random.Random(seed).randbytes(total_bytes))
    return _buffers[key]


def order(n, pattern, seed):
    idx = list(range(n))
    if pattern == "rand":
        random.Random(seed).shuffle(idx)
    return idx


def run_once(level, size_mb, bs, pattern, sync="end", full_stripe=False,
             threads=1, rep=0):
    total = size_mb * MB
    n = total // bs
    shutil.rmtree(WORKDIR, ignore_errors=True)
    raid = make_raid(level, WORKDIR, total, bs, sync_each_write=False)
    old = payload(total, 999)    # pre-fill data
    new = payload(total, 1)      # benchmark data  (both cached between runs)

    # ---- pre-fill (untimed) ----------------------------------------------
    for b in range(n):
        raid.write(b, old[b * bs:(b + 1) * bs])
    raid.drop_cache()
    raid.reset_counts()
    raid.sync_each_write = (sync == "each")

    # ---- timed write phase -------------------------------------------------
    wr_order = order(n, pattern, 1000 + rep)
    t0 = time.perf_counter()
    if full_stripe:                       # RAID 4/5 sequential full-stripe writes
        k = raid.DATA_PER_STRIPE
        for s in range(n // k):
            raid.write_full_stripe(s, [new[(s * k + i) * bs:(s * k + i + 1) * bs]
                                       for i in range(k)])
    elif threads == 1:
        for b in wr_order:
            raid.write(b, new[b * bs:(b + 1) * bs])
    else:
        def worker(chunk):
            for b in chunk:
                raid.write(b, new[b * bs:(b + 1) * bs])
        ts = [threading.Thread(target=worker, args=(wr_order[i::threads],))
              for i in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    if sync == "end":
        raid.sync()
    t_write = time.perf_counter() - t0
    w_reads, w_writes = raid.io_counts()

    # ---- timed read + verify phase ----------------------------------------
    raid.sync_each_write = False
    raid.drop_cache()
    raid.reset_counts()
    rd_order = order(n, pattern, 2000 + rep)
    errors = 0
    t0 = time.perf_counter()
    for b in rd_order:
        if raid.read(b) != bytes(new[b * bs:(b + 1) * bs]):
            errors += 1
    t_read = time.perf_counter() - t0
    r_reads, _ = raid.io_counts()

    parity_bad = raid.verify_parity() if hasattr(raid, "verify_parity") else 0
    phys_bytes = sum(os.path.getsize(d.path) for d in raid.disks)
    raid.close()
    shutil.rmtree(WORKDIR, ignore_errors=True)

    return dict(level=level, size_mb=size_mb, block_size=bs, pattern=pattern,
                sync=sync, full_stripe=full_stripe, threads=threads, rep=rep,
                blocks=n, write_s=t_write, read_s=t_read,
                write_MBps=size_mb / t_write, read_MBps=size_mb / t_read,
                write_us_per_block=t_write / n * 1e6,
                read_us_per_block=t_read / n * 1e6,
                phys_reads_per_write=w_reads / n, phys_writes_per_write=w_writes / n,
                phys_reads_per_read=r_reads / n,
                verify_errors=errors, parity_bad_stripes=parity_bad,
                physical_bytes=phys_bytes, logical_bytes=total)


# ---------------------------------------------------------------------------
def experiments(quick):
    R = 2 if quick else 5           # repetitions for main experiment
    R2 = 1 if quick else 3          # repetitions for sweeps
    big = 10 if quick else 100
    small = 2 if quick else 8
    lv = list(LEVELS)
    E = {}
    E["E1"] = ("Main: 100 MB, 4 KB blocks, sequential vs random, fsync at end",
               [dict(level=l, size_mb=big, bs=4096, pattern=p, rep=r)
                for p in ("seq", "rand") for l in lv for r in range(R)])
    E["E2"] = ("Block-size sweep (100 MB)",
               [dict(level=l, size_mb=big, bs=bs, pattern=p, rep=r)
                for bs in (512, 4096, 65536) for p in ("seq", "rand")
                for l in lv for r in range(R2)])
    E["E3"] = ("Workload-size sweep (4 KB blocks)",
               [dict(level=l, size_mb=s, bs=4096, pattern=p, rep=r)
                for s in ((2, 5, 10) if quick else (10, 50, 100, 200))
                for p in ("seq", "rand") for l in lv for r in range(R2)])
    E["E4"] = ("Synchronous writes: fsync every physical write (8 MB, 4 KB)",
               [dict(level=l, size_mb=small, bs=4096, pattern=p, sync="each", rep=r)
                for p in ("seq", "rand") for l in lv for r in range(R2)])
    E["E5"] = ("RAID 4/5 full-stripe vs read-modify-write sequential writes (4 KB)",
               [dict(level=l, size_mb=sz, bs=4096, pattern="seq", sync=sy,
                     full_stripe=fs, rep=r)
                for (sz, sy) in ((big, "end"), (small, "each"))
                for l in ("RAID4", "RAID5") for fs in (False, True) for r in range(R2)])
    E["E6"] = ("Concurrent random small writes, fsync each write (8 MB, 4 KB)",
               [dict(level=l, size_mb=small, bs=4096, pattern="rand", sync="each",
                     threads=t, rep=r)
                for t in (1, 4, 8) for l in lv for r in range(R2)])
    return E


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    os.makedirs(OUTDIR, exist_ok=True)
    out = os.path.join(OUTDIR, "results-quick.json" if args.quick else "results.json")
    results = json.load(open(out)) if os.path.exists(out) else {}
    for name, (desc, runs) in experiments(args.quick).items():
        if args.only and name not in args.only:
            continue
        print(f"\n== {name}: {desc} ({len(runs)} runs)", flush=True)
        rows = []
        for cfg in runs:
            kw = dict(cfg)
            kw["bs"] = kw.pop("bs")
            res = run_once(**kw)
            res["experiment"] = name
            rows.append(res)
            print(f"  {res['level']} {res['size_mb']}MB bs={res['block_size']} "
                  f"{res['pattern']} sync={res['sync']} fs={res['full_stripe']} "
                  f"thr={res['threads']} rep={res['rep']}: "
                  f"W {res['write_MBps']:8.1f} MB/s  R {res['read_MBps']:8.1f} MB/s  "
                  f"err={res['verify_errors']} parity_bad={res['parity_bad_stripes']}",
                  flush=True)
            if res["verify_errors"] or res["parity_bad_stripes"]:
                print("  !!! CORRECTNESS FAILURE", file=sys.stderr)
        results[name] = dict(description=desc, runs=rows)
        json.dump(results, open(out, "w"), indent=1)
    print(f"\nraw results written to {out}")


if __name__ == "__main__":
    main()
