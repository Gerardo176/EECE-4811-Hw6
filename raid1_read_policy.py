"""raid1_read_policy.py - diagnostic: how the RAID 1 read policy affects
sequential read throughput (100 MB, 4 KB blocks, cold cache, 3 reps).

  round-robin   : block A read from disk A mod 5  (policy used in raid.py)
  single-disk   : every read from disk 0
  chunked-RR    : block A read from disk (A div 256) mod 5, i.e. each mirror
                  serves a contiguous 1 MB run before moving to the next
"""
import os, shutil, statistics, time, random
from raid import make_raid

HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "disks")
BS, SIZE = 4096, 100 * 2**20
POLICIES = {
    "round-robin": lambda b: b % 5,
    "single-disk": lambda b: 0,
    "chunked-RR (1 MB)": lambda b: (b // 256) % 5,
}
shutil.rmtree(D, ignore_errors=True)
r = make_raid("RAID1", D, SIZE, BS)
data = random.Random(1).randbytes(SIZE)
for b in range(r.capacity_blocks):
    r.write(b, data[b * BS:(b + 1) * BS])
print(f"{'policy':20s} read MB/s (mean ± sd of 3)")
for name, pick in POLICIES.items():
    res = []
    for _ in range(3):
        r.drop_cache()
        t = time.perf_counter()
        for b in range(r.capacity_blocks):
            assert r.disks[pick(b)].read_block(b) == data[b * BS:(b + 1) * BS]
        res.append(100 / (time.perf_counter() - t))
    print(f"{name:20s} {statistics.mean(res):7.1f} ± {statistics.stdev(res):.1f}")
r.close()
shutil.rmtree(D, ignore_errors=True)
