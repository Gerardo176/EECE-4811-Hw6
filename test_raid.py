"""Correctness tests: python3 test_raid.py"""
import os
import random
import shutil
import tempfile

from raid import LEVELS, RAID5, RAID4, NUM_DISKS, make_raid, xor_bytes

BS = 512


def check_level(level, tmp):
    r = make_raid(level, os.path.join(tmp, level), 400 * BS, BS)
    rng = random.Random(1)
    shadow = {}
    # unwritten blocks are zero
    assert r.read(7) == b"\0" * BS
    # random writes, with many overwrites of the same blocks
    for _ in range(3000):
        b = rng.randrange(r.capacity_blocks)
        data = rng.randbytes(BS)
        r.write(b, data)
        shadow[b] = data
    for b in range(r.capacity_blocks):
        assert r.read(b) == shadow.get(b, b"\0" * BS), (level, b)
    if level in ("RAID4", "RAID5"):
        assert r.verify_parity() == 0, f"{level} parity broken"
        # full-stripe writes must keep parity consistent too
        r.write_full_stripe(3, [rng.randbytes(BS) for _ in range(4)])
        assert r.verify_parity() == 0
        # reconstruct a "failed" disk from the others to prove parity is useful
        for s in range(r.blocks_per_disk):
            lost = r.disks[2].read_block(s)
            rebuilt = xor_bytes(*(r.disks[d].read_block(s) for d in range(NUM_DISKS) if d != 2))
            assert lost == rebuilt
    if level == "RAID1":
        for b in range(r.capacity_blocks):
            copies = {r.disks[d].read_block(b) for d in range(NUM_DISKS)}
            assert len(copies) == 1, "mirrors diverged"
    r.close()
    print(f"{level}: OK (capacity {r.capacity_blocks} blocks, "
          f"{r.blocks_per_disk} blocks/disk)")


def check_layout(tmp):
    # RAID 5 layout must match OSTEP Figure 38.7 (left-symmetric)
    r = RAID5(os.path.join(tmp, "l5"), 10, BS)
    grid = [[None] * NUM_DISKS for _ in range(5)]
    for b in range(20):
        d, off = r.mapping(b)
        grid[off][d] = b
    for s in range(5):
        grid[s][r.parity_disk(s)] = "P"
    expected = [[0, 1, 2, 3, "P"], [5, 6, 7, "P", 4], [10, 11, "P", 8, 9],
                [15, "P", 12, 13, 14], ["P", 16, 17, 18, 19]]
    assert grid == expected, grid
    r4 = RAID4(os.path.join(tmp, "l4"), 10, BS)
    assert [r4.mapping(b) for b in range(6)] == [(0, 0), (1, 0), (2, 0), (3, 0), (0, 1), (1, 1)]
    print("Layouts: OK")
    for row in grid:
        print("   ", " ".join(f"{x:>3}" for x in row))


if __name__ == "__main__":
    tmp = tempfile.mkdtemp()
    try:
        check_layout(tmp)
        for lvl in LEVELS:
            check_level(lvl, tmp)
    finally:
        shutil.rmtree(tmp)
