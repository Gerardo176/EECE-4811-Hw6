# RAID Simulation and Performance Analysis

Language: **Python 3.13** 
Platform: Linux 6.18, 4 vCPU, ext4 on one virtual block device (`/dev/vda`)

| File | Purpose |
|---|---|
| `raid.py` | Simulated disks + RAID 0 / 1 / 4 / 5 (`write(block, data)`, `read(block)`) |
| `test_raid.py` | Correctness tests (layout, overwrites, parity, rebuild of a lost disk) |
| `benchmark.py` | Experiments E1–E6 → `results/results.json` |
| `summarize.py` | Builds `results/summary.md` (all tables, mean ± std-dev) |
| `raid1_read_policy.py` | Diagnostic for the RAID 1 sequential-read result |
| `results/` | Raw data, logs, summaries; `results/v1_unoptimized/` = first run (see §4.5) |

Run with:

```bash
python3 test_raid.py
```

```bash
python3 benchmark.py && python3 summarize.py
```

---

## 1. Part A: Implementation

### 1.1 Simulated disks

Each disk is a regular file, `disk0.dat` … `disk4.dat`. All five are the same size and are created zero-filled with `ftruncate`, so unwritten blocks read as zeros and the initial parity is already correct (XOR of zeros = 0). A `Disk` reads and writes whole blocks at a block offset with `os.pread`/`os.pwrite`. Each disk also has a lock, because a physical disk serves one request at a time. The lock only matters in the multi-threaded experiment (E6).

**Block size** defaults to 4 KB, and E2 also runs 512 B and 64 KB. **Chunk size** is one block for every level.

### 1.2 Mappings (N = 5 disks, A = logical block number)

| Level | Data location | Redundancy | Ops per logical write |
|---|---|---|---|
| **RAID 0** | disk = A mod 5, offset = A div 5 | none | 1 write |
| **RAID 1** | offset A on **all five disks** (five-way mirror); reads go to disk A mod 5 (round-robin) | 4 extra copies | 5 writes |
| **RAID 4** | stripe = A div 4, disk = A mod 4, offset = stripe | parity on disk 4 | 2 reads + 2 writes |
| **RAID 5** | stripe = A div 4; parity disk p = 4 − (stripe mod 5); data index i → disk (p+1+i) mod 5 | rotating parity | 2 reads + 2 writes |

**RAID 1 configuration.** I used five-way mirroring as the assignment allows. Every logical block is stored at the same offset on all five disks, so the array survives any four disk failures but holds only one disk's worth of data. Reads are spread round-robin across the mirrors (disk = A mod 5). §3.2 shows that this read policy has a large effect on performance.

**RAID 5 layout.** RAID 5 uses the left-symmetric layout from OSTEP Figure 38.7. `test_raid.py` checks it:

```
disk:  0   1   2   3   4
       0   1   2   3   P
       5   6   7   P   4
      10  11   P   8   9
      15   P  12  13  14
       P  16  17  18  19
```

### 1.3 Parity and overwrites

`write()` on RAID 4/5 uses **subtractive parity** (read-modify-write), so any block can be overwritten and the parity stays correct:

```
D_old = read(data disk);  P_old = read(parity disk)
P_new = P_old XOR D_old XOR D_new
write(data disk, D_new);  write(parity disk, P_new)
```

This costs 2 reads and 2 writes no matter how many disks the array has. Each read-modify-write holds a per-stripe lock, so concurrent writers (E6) cannot corrupt parity.

There is also an optional `write_full_stripe()` for RAID 4/5. It writes 4 new data blocks and computes parity directly as their XOR, with no reads, which is OSTEP's *full-stripe write*. E5 uses it.

### 1.4 Correctness evidence

* `test_raid.py`, for every level: 3000 random writes with many overwrites of the same blocks, then a full read-back compared against a shadow copy.
  * RAID 4/5 additionally: every stripe must XOR to 0. Each block of a "failed" disk is then rebuilt from the other four and must match the original.
  * RAID 1 additionally: all five mirrors must be identical.
* Every benchmark run checks every block it reads against the data written. It also checks the parity of every stripe after the run.
* Result: **292 benchmark runs, 0 read mismatches, 0 bad parity stripes.**

---

## 2. Part B: Benchmark methodology

### 2.1 Policy (identical for all levels)

1. **Same logical data.** Every level stores the same logical bytes, for example 100 MB. Disk files are sized from each level's efficiency: RAID 1 uses 5 × 100 MB of files and RAID 4/5 use 5 × 25 MB.
2. **Writes are true overwrites.** Before each timed write phase the array is pre-filled with different data, fsync'ed, and evicted from the page cache with `posix_fadvise(DONTNEED)`. So the timed phase overwrites real data, and RAID 4/5 read-modify-write must fetch the old data and parity from storage, not from RAM.
3. **Sync policies.** These are the "consistent policy" the assignment asks for:
   * **`end`** (default): the write phase finishes with `fsync()` on all five disks, and that time is included. This measures how fast the data becomes durable.
   * **`each`** (E4–E6): every physical block write is fsync'ed before the request completes, as with synchronous writes such as databases. This exposes the cost per physical I/O.
4. **Cold reads.** Before the read phase all disks are fsync'ed and evicted from the page cache.
5. **What is timed.** The write phase, then the read-and-verify phase. Throughput is logical MB/s, and latency is µs per logical block. Test data is generated before timing starts.
6. **Repetitions.** E1 uses 5 repetitions and all other experiments use 3. Random orders use a different seed each repetition. Tables show mean ± sample standard deviation.

Measured base costs on this machine: a 4 KB `pwrite` into the page cache costs about 1 µs, and a 4 KB write plus `fsync` costs about 95–190 µs.

### 2.2 Workloads and why they reveal RAID differences

| Exp. | Workload | What it reveals |
|---|---|---|
| **E1** | 100 MB, 4 KB, sequential vs random, write + read | Main comparison. Random small writes trigger RAID 4/5 read-modify-write (the "small-write problem"). Sequential access shows streaming behaviour and readahead. |
| **E2** | 512 B / 4 KB / 64 KB blocks | Per-request overhead vs bandwidth. Small blocks are dominated by fixed per-operation costs, large blocks by bytes moved. Parity cost scales with block size. |
| **E3** | 10 / 50 / 100 / 200 MB | Whether results are steady state or depend on caches and warm-up. |
| **E4** | fsync on every physical write | Removes write-back caching, so the cost per logical write equals the number of physical I/Os. This makes OSTEP's I/O counting visible. |
| **E5** | RAID 4/5 sequential: per-block RMW vs full-stripe writes | Tests OSTEP's claim that full-stripe writes avoid the parity read penalty. |
| **E6** | 1 / 4 / 8 threads of random synchronous writes | Concurrency is what shows the **RAID 4 parity-disk bottleneck** and RAID 5's fix. A single-threaded benchmark cannot show it. |

---

## 3. Results

Full tables are in `results/summary.md`. Below, **S** means sequential and **R** means random.

### 3.1 Storage efficiency

| Level | Usable capacity (5 disks of size C) | Efficiency | Physical MB for 100 MB data | Failures tolerated |
|---|---|---|---|---|
| RAID 0 | 5C | 100% | 100 | 0 |
| RAID 1 (5-way) | 1C | 20% | 500 | 4 |
| RAID 4 | 4C | 80% | 125 | 1 |
| RAID 5 | 4C | 80% | 125 | 1 |

The physical sizes were measured from the disk files.

### 3.2 E1: main results (100 MB, 4 KB blocks, sync at end, 5 runs)

| | RAID 0 | RAID 1 | RAID 4 | RAID 5 |
|---|---|---|---|---|
| **Seq write** MB/s | 486 ± 34 | 108 ± 1 | 294 ± 14 | 278 ± 27 |
| **Rand write** MB/s | 469 ± 8 | 102 ± 4 | 74 ± 4 | 73 ± 1 |
| **Seq read** MB/s | 1456 ± 296 | 94 ± 2 | 1424 ± 284 | 1354 ± 202 |
| **Rand read** MB/s | 141 ± 9 | 101 ± 9 | 136 ± 3 | 111 ± 5 |
| Rand write µs/block | 8.3 | 38.5 | 53.1 | 53.5 |
| Rand read µs/block | 27.9 | 38.8 | 28.7 | 35.2 |

The RAID 1 sequential-read result is low enough to need its own diagnostic (`raid1_read_policy.py`: 100 MB, sequential, cold cache):

| RAID 1 read policy | Read MB/s |
|---|---|
| round-robin per block (disk = A mod 5), the policy in `raid.py` | 99 ± 4 |
| always read disk 0 | 1634 ± 71 |
| round-robin per 1 MB chunk | 1276 ± 65 |

### 3.3 E4: one fsync per physical write (8 MB, 4 KB, 3 runs)

| | RAID 0 | RAID 1 | RAID 4 | RAID 5 |
|---|---|---|---|---|
| Seq write µs/block | 102 | 468 | 226 | 203 |
| Rand write µs/block | 96 | 486 | 248 | 256 |
| Physical writes / logical write | 1 | 5 | 2 | 2 |
| Physical reads / logical write | 0 | 0 | 2 | 2 |

### 3.4 E5: RAID 4/5 sequential writes, RMW vs full stripe (MB/s)

| | RAID 4 RMW | RAID 4 full-stripe | RAID 5 RMW | RAID 5 full-stripe |
|---|---|---|---|---|
| 100 MB, sync at end | 310 ± 12 | 358 ± 2 | 316 ± 4 | 367 ± 15 |
| 8 MB, fsync each write | 17.6 ± 0.4 | 29.9 ± 2.2 | 20.2 ± 1.3 | 28.6 ± 1.6 |
| Physical I/Os per logical block | 2 R + 2 W | 0 R + 1.25 W | 2 R + 2 W | 0 R + 1.25 W |

### 3.5 E6: concurrent random synchronous writes (MB/s)

| Threads | RAID 0 | RAID 1 | RAID 4 | RAID 5 |
|---|---|---|---|---|
| 1 | 42.9 ± 4.3 | 7.8 ± 0.5 | 15.4 ± 0.3 | 14.3 ± 0.9 |
| 4 | 63.0 ± 1.1 | 12.5 ± 1.9 | 16.0 ± 0.6 | **19.1 ± 1.0** |
| 8 | 59.5 ± 12.0 | 14.7 ± 0.5 | 16.1 ± 0.9 | **17.6 ± 0.9** |
| Speed-up, 1 → 4 threads | 1.47× | 1.60× | **1.04×** | **1.34×** |

### 3.6 E2 and E3: block size and workload size (MB/s, 3 runs)

| Block | Pattern | RAID 0 W / R | RAID 1 W / R | RAID 4 W / R | RAID 5 W / R |
|---|---|---|---|---|---|
| 512 B | seq | 72 / 396 | 17 / 327 | 66 / 373 | 64 / 346 |
| 512 B | rand | 64 / 91 | 15 / 27 | 36 / 92 | 37 / 83 |
| 4 KB | seq | 479 / 1645 | 112 / 97 | 316 / 1549 | 298 / 1488 |
| 4 KB | rand | 474 / 135 | 104 / 98 | 74 / 135 | 72 / 112 |
| 64 KB | seq | 1060 / 2744 | 238 / 673 | 583 / 2796 | 611 / 2969 |
| 64 KB | rand | 1125 / 759 | 242 / 803 | 286 / 747 | 278 / 754 |

**Workload size (E3).** From 10 MB to 200 MB the throughput of every level stayed flat within noise. For example, RAID 5 random writes were 73.0, 73.8, 75.6 and 72.9 MB/s. Because the policy fsyncs and evicts the cache, the results are steady state and not artifacts of the page cache.

---

## 4. Analysis

### 4.1 Write performance

**RAID 0 is fastest.** One logical write is one physical write. With sync at end, sequential and random writes are nearly equal (486 vs 469 MB/s). The kernel buffers the writes in the page cache and sorts them before writeback, so the device sees large, mostly sequential writes in both cases.

**RAID 1 costs about 5×, as predicted.** Five-way mirroring writes every block five times. Throughput is 0.22× RAID 0 (108 vs 486 MB/s), close to the theoretical 1/5. E4 shows this most clearly: 468–486 µs per write is about 5 × 96 µs, the cost of one synchronous RAID 0 write.

**RAID 4/5 depend heavily on the access pattern.**
* *Random small writes are the slowest of all levels* at 73 MB/s, 53 µs per block, which is 6.4× slower than RAID 0. Each write does 2 reads and 2 writes (measured: exactly 2.00 and 2.00 per logical write). The two reads of old data and old parity must come from the device because the cache was evicted, and they block the request. This is OSTEP's small-write problem.
* *Sequential writes reach 60% of RAID 0* (294 MB/s), even though each block still goes through read-modify-write. Readahead keeps the old-data and parity reads in cache. More importantly, the four parity updates of a stripe all go to the same page, which is written to disk only once. Write-back caching therefore turns RMW into roughly full-stripe traffic.
* *Full-stripe writes (E5)* avoid the reads altogether and write 1.25 physical blocks per logical block. They reached 358–367 MB/s, about 0.75× RAID 0. That is close to OSTEP's (N−1)/N = 0.8. With fsync on every write, full-stripe writes were 1.4–1.7× faster than RMW.
* *RAID 4 vs RAID 5* are the same single-threaded (73.7 vs 73.0 MB/s random). They differ only under concurrency (E6). RAID 5 scales 1.34× from 1 to 4 threads, but RAID 4 stays flat (1.04×), because every write in RAID 4 needs the single parity disk, whose lock serializes all writers. This is exactly the RAID 4 bottleneck OSTEP describes.

### 4.2 Read performance

**Sequential.** RAID 0, 4 and 5 all reach roughly 1350–1650 MB/s. Each of their disks is read at increasing offsets, so Linux readahead streams large chunks.
* RAID 4 never touches the parity disk on reads, so it equals RAID 0.
* RAID 5 is slightly lower (about 1354 MB/s). Each disk skips one offset out of every five, where its parity sits.

**RAID 1 sequential reads are the worst result (94 MB/s).** This is the textbook effect, amplified.
* OSTEP explains that mirrored sequential reads get only half the bandwidth, because each disk skips the blocks served by the other copy.
* With round-robin over 5 mirrors, each disk reads offsets 0, 5, 10, … A strided pattern does not trigger Linux readahead, so every 4 KB block becomes a separate device read.
* The diagnostic confirms it. The same RAID 1 data reads at 1634 MB/s from a single disk and at 1276 MB/s with 1 MB round-robin chunks.
* At 512 B blocks the stride (2.5 KB) fits inside a page, and RAID 1 sequential reads recover to 327 MB/s.
* So mirrors should be assigned by large chunks or by request stream, not by individual block.

**Random reads.** Every level needs exactly one physical read per logical read (measured 1.00).
* RAID 0 (141 MB/s) and RAID 4 (136 MB/s) are equal.
* RAID 5 (111 MB/s) and RAID 1 (101 MB/s) are consistently slower. My explanation: the 100 MB of useful data is spread over more physical bytes. RAID 0 and RAID 4 read only data files (100 MB, since RAID 4 never reads parity). RAID 5's data is interleaved with parity across 125 MB, and RAID 1's reads are spread over 500 MB of files.
* Larger spread means less locality for the virtual disk's own caching and prefetching.
* The ordering by data density (100% ≈ 100% > 80% > 20%) matches the ordering of the results. It is strongest at 512 B, where RAID 1 random reads drop to 27 MB/s.

### 4.3 Storage efficiency

| Level | Efficiency | Trade-off |
|---|---|---|
| RAID 0 | 100% | No fault tolerance at all |
| RAID 1 (5-way) | 20% | Survives 4 failures. Most expensive in capacity and in write bandwidth. |
| RAID 4 / 5 | 80% ((N−1)/N) | Survives 1 failure, at the price of the small-write penalty |

### 4.4 Comparison with the textbook (OSTEP Table 38.8)

Ratios are relative to RAID 0 and come from E1/E5. For RAID 1, OSTEP's formulas assume 2-way mirroring. With 5-way mirroring a write costs 5 I/Os, so the expected write ratio is 1/5.

| Metric | RAID 1 theory → observed | RAID 4 theory → observed | RAID 5 theory → observed |
|---|---|---|---|
| Seq write | 0.20 → **0.22** ✓ | 0.80 → 0.60 (RMW), **0.74** (full stripe) ✓ | 0.80 → 0.57 (RMW), **0.75** (full stripe) ✓ |
| Rand write | 0.20 → **0.22** ✓ | 0.10 → 0.16 ✗ | 0.25 → 0.16 ✗ |
| Seq read | ≤0.5 → 0.06 (amplified) | 0.80 → 0.98 | 0.80 → 0.93 |
| Rand read | 1.00 → 0.72 | 0.80 → 0.97 | 1.00 → 0.79 |
| Small-write latency | 1 T → 5 T* | 2 T → ~2.6 T | 2 T → ~2.7 T |

\*With synchronous writes and requests issued one after another. Real RAID 1 hardware writes the mirrors in parallel, so the latency would be about 1 T.

**What matches.** The I/O counts match exactly: 1 / 5 / 2 + 2 / 2 + 2 physical operations per write. The relative write costs of mirroring and parity match. Full-stripe writes approach (N−1)/N of RAID 0. The RAID 4 parity bottleneck appears only under concurrency and is relieved by RAID 5. The RAID 1 sequential-read penalty matches qualitatively.

**What differs, and why:**
1. **There is no real disk parallelism.** All five "disks" are files on one virtual block device. OSTEP's throughput figures assume N independent spindles serving requests in parallel (the N·S and N·R terms). Here a request to "disk 3" competes with a request to "disk 1" for the same device. So RAID 0 cannot be 5× faster than one disk, and random-read throughput is about the same for every level.
2. **The benchmark is single-threaded** except in E6. OSTEP's random-throughput figures assume many outstanding requests. With one request at a time only latency matters, so RAID 4 and RAID 5 look the same (both pay 2 reads and 2 writes per request), and the RAID 5 advantage stays hidden. E6 brings back part of the difference. The gains are modest because the ext4 journal commit performed on each `fsync` is shared by the whole filesystem and partly serializes all five "disks".
3. **The OS page cache and readahead** sit between the RAID code and the device. Write-back caching merges repeated parity writes, which is why sequential RMW behaves almost like full-stripe writes. Readahead makes contiguous reads very cheap and strided reads expensive. That magnifies RAID 1's round-robin penalty far beyond OSTEP's factor of 2.
4. **My RAID layer issues I/Os one after another.** For example, the five mirror writes and the two RMW reads are not issued concurrently. Under fsync-per-write this turns OSTEP's latency of about 2T (reads in parallel, then writes in parallel) into a sum of sequential operations.
5. **There are no mechanical seeks.** The virtual device is likely flash-backed, so "random" costs a device round trip rather than a 10 ms seek, which shrinks the gap between sequential and random access.

### 4.5 A measurement lesson (first run, `results/v1_unoptimized/`)

The first full run computed XOR with Python big integers (about 19 µs per write) and compared `bytes == memoryview` during verification (6.6 µs per block). As a result:
* sequential reads of RAID 0/4/5 were capped at about 500 MB/s by the CPU;
* RAID 4/5 sequential writes were capped at about 150 MB/s.

The bottleneck was the Python interpreter, not the RAID design. After switching to NumPy XOR (2.8 µs) and comparing `bytes` to `bytes` (0.19 µs), sequential reads rose about 3× and RAID 4/5 sequential writes about 2×. The relative conclusions held, apart from the RAID 4/5 sequential-write gap.

The lesson: in a file-based simulation, CPU cost per operation can easily dominate. Comparing results with a warm page cache (`results/python_overhead.txt`) is a quick way to check whether a benchmark is I/O-bound.

### 4.6 Summary

* **RAID 0:** fastest everywhere, 100% capacity, no redundancy.
* **RAID 1 (5-way):** writes cost about 5× (0.2 of RAID 0), 20% capacity, best fault tolerance. Its read performance depends strongly on how reads are spread across mirrors.
* **RAID 4/5:** 80% capacity with single-failure tolerance.
  * Reads are as good as RAID 0.
  * Large or full-stripe writes reach about 0.75 of RAID 0.
  * Random small writes are the worst case (2 reads + 2 writes each).
  * RAID 5 beats RAID 4 only under concurrent writes, where RAID 4's parity disk becomes a serialization point.
