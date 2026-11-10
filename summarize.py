"""summarize.py - turn results/results.json into markdown tables (results/summary.md)."""
import json
import os
import statistics
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "results", "results.json")
dst = os.path.join(os.path.dirname(src), "summary.md")
data = json.load(open(src))
LV = ["RAID0", "RAID1", "RAID4", "RAID5"]


def ms(vals):
    m = statistics.mean(vals)
    s = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return f"{m:.1f} ± {s:.1f}"


def group(runs, keys):
    g = defaultdict(list)
    for r in runs:
        g[tuple(r[k] for k in keys)].append(r)
    return g


out = []
P = out.append
total_err = sum(r["verify_errors"] + r["parity_bad_stripes"]
                for e in data.values() for r in e["runs"])
nruns = sum(len(e["runs"]) for e in data.values())
P(f"# Benchmark summary\n\n{nruns} runs; verification errors + bad parity stripes "
  f"across all runs: **{total_err}**\n\nValues are mean ± sample std-dev over repetitions. "
  "MB/s is *logical* (user) data throughput.\n")

# Storage efficiency (from E1)
if "E1" in data:
    P("## Storage efficiency (E1 logical data set, 4 KB blocks)\n")
    P("| Level | Logical MB | Physical MB on 5 disks | Efficiency | Phys. writes / logical write | Phys. reads / logical write (RMW) |")
    P("|---|---|---|---|---|---|")
    seen = set()
    for r in data["E1"]["runs"]:
        if r["level"] in seen or r["pattern"] != "rand":
            continue
        seen.add(r["level"])
        P(f"| {r['level']} | {r['logical_bytes']/2**20:.0f} | {r['physical_bytes']/2**20:.0f} | "
          f"{r['logical_bytes']/r['physical_bytes']*100:.0f}% | {r['phys_writes_per_write']:.2f} | "
          f"{r['phys_reads_per_write']:.2f} |")
    P("")


def table(name, keys, label):
    if name not in data:
        return
    e = data[name]
    P(f"## {name}: {e['description']}\n")
    reps = {len(v) for v in group(e["runs"], keys + ["level"]).values()}
    P(f"Repetitions per cell: {sorted(reps)}\n")
    for metric, unit in (("write_MBps", "Write MB/s"), ("read_MBps", "Read MB/s"),
                         ("write_us_per_block", "Write µs/block"),
                         ("read_us_per_block", "Read µs/block")):
        P(f"**{unit}**\n")
        P(f"| {label} | " + " | ".join(LV) + " |")
        P("|---|" + "---|" * len(LV))
        g = group(e["runs"], keys + ["level"])
        for k in sorted({kk[:-1] for kk in g}, key=lambda x: tuple(str(i).rjust(8) for i in x)):
            cells = [ms([r[metric] for r in g[k + (l,)]]) if k + (l,) in g else "–" for l in LV]
            P("| " + ", ".join(str(i) for i in k) + " | " + " | ".join(cells) + " |")
        P("")


table("E1", ["pattern"], "pattern")
table("E2", ["block_size", "pattern"], "block size, pattern")
table("E3", ["size_mb", "pattern"], "MB, pattern")
table("E4", ["pattern"], "pattern")
table("E6", ["threads"], "threads")

if "E5" in data:
    e = data["E5"]
    P(f"## E5: {e['description']}\n")
    P("| Level | sync | size MB | Method | Write MB/s | Phys reads/blk | Phys writes/blk |")
    P("|---|---|---|---|---|---|---|")
    g = group(e["runs"], ["level", "sync", "size_mb", "full_stripe"])
    for k in sorted(g):
        rs = g[k]
        P(f"| {k[0]} | {k[1]} | {k[2]} | {'full-stripe' if k[3] else 'RMW per block'} | "
          f"{ms([r['write_MBps'] for r in rs])} | {rs[0]['phys_reads_per_write']:.2f} | "
          f"{rs[0]['phys_writes_per_write']:.2f} |")
    P("")

open(dst, "w").write("\n".join(out))
print("\n".join(out))
