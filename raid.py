"""
raid.py - RAID 0 / 1 / 4 / 5 simulated on top of five regular files.

Each "disk" is a file (disk0.dat ... disk4.dat) accessed with positional
I/O (os.pread / os.pwrite) at block granularity. All RAID levels expose the
same interface:

    raid.write(block_num, data)   # data must be exactly block_size bytes
    raid.read(block_num) -> bytes
    raid.sync()                   # fsync every disk
    raid.capacity_blocks          # number of usable logical blocks

Unwritten blocks read back as zeros (the files are created sparse/zeroed),
which also means parity is initially correct (XOR of zeros = 0).
"""

import os
import threading

NUM_DISKS = 5


try:
    import numpy as _np
except ImportError:          # pure-Python fallback
    _np = None


def xor_bytes(*chunks) -> bytes:
    """XOR equally sized byte strings (bytes / memoryview).

    Uses NumPy's vectorised XOR when available (~3 us for three 4 KB blocks);
    otherwise falls back to XOR-ing Python big ints (~18 us).
    """
    if _np is not None:
        acc = _np.frombuffer(chunks[0], dtype=_np.uint64).copy() \
            if len(chunks[0]) % 8 == 0 else _np.frombuffer(chunks[0], dtype=_np.uint8).copy()
        dt = acc.dtype
        for c in chunks[1:]:
            _np.bitwise_xor(acc, _np.frombuffer(c, dtype=dt), out=acc)
        return acc.tobytes()
    n = len(chunks[0])
    acc = 0
    for c in chunks:
        acc ^= int.from_bytes(c, "little")
    return acc.to_bytes(n, "little")


class Disk:
    """A simulated physical disk backed by a regular file."""

    def __init__(self, path: str, num_blocks: int, block_size: int):
        self.path = path
        self.block_size = block_size
        self.num_blocks = num_blocks
        # Fresh, zero-filled (sparse) file of the right size.
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        os.ftruncate(self.fd, num_blocks * block_size)
        self.reads = 0
        self.writes = 0
        # A physical disk services one request at a time. The lock models that
        # (it matters only for the multi-threaded experiment).
        self.lock = threading.Lock()

    def read_block(self, offset: int) -> bytes:
        with self.lock:
            self.reads += 1
            data = os.pread(self.fd, self.block_size, offset * self.block_size)
        if len(data) < self.block_size:          # should not happen (file pre-sized)
            data += b"\0" * (self.block_size - len(data))
        return data

    def write_block(self, offset: int, data: bytes, sync: bool = False) -> None:
        """Write one block; with sync=True the write is fsync'ed before the
        disk is released (i.e. the request is not "done" until durable)."""
        with self.lock:
            self.writes += 1
            os.pwrite(self.fd, data, offset * self.block_size)
            if sync:
                os.fsync(self.fd)

    def sync(self) -> None:
        os.fsync(self.fd)

    def drop_cache(self) -> None:
        """Ask the kernel to evict this file's (clean) pages from the page
        cache so later reads hit the storage device instead of RAM."""
        os.fsync(self.fd)
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_DONTNEED)

    def close(self) -> None:
        os.close(self.fd)


class RAIDBase:
    level = "?"

    def __init__(self, directory: str, blocks_per_disk: int, block_size: int,
                 sync_each_write: bool = False):
        os.makedirs(directory, exist_ok=True)
        self.block_size = block_size
        self.blocks_per_disk = blocks_per_disk
        self.sync_each_write = sync_each_write
        self.disks = [Disk(os.path.join(directory, f"disk{i}.dat"),
                           blocks_per_disk, block_size)
                      for i in range(NUM_DISKS)]

    # --- helpers ---------------------------------------------------------
    def _check(self, block_num, data=None):
        if not 0 <= block_num < self.capacity_blocks:
            raise IndexError(f"logical block {block_num} out of range")
        if data is not None and len(data) != self.block_size:
            raise ValueError(f"data must be exactly {self.block_size} bytes")

    # --- common API ------------------------------------------------------
    def sync(self):
        for d in self.disks:
            d.sync()

    def drop_cache(self):
        for d in self.disks:
            d.drop_cache()

    def close(self):
        for d in self.disks:
            d.close()

    def io_counts(self):
        return (sum(d.reads for d in self.disks), sum(d.writes for d in self.disks))

    def reset_counts(self):
        for d in self.disks:
            d.reads = d.writes = 0

    @property
    def capacity_bytes(self):
        return self.capacity_blocks * self.block_size

    def mapping(self, block_num):
        """Return (disk, offset) holding a logical block (first copy for RAID 1)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# RAID 0: striping, no redundancy. Chunk size = 1 block.
#   disk   = A mod N
#   offset = A div N
# ---------------------------------------------------------------------------
class RAID0(RAIDBase):
    level = "RAID0"

    @property
    def capacity_blocks(self):
        return NUM_DISKS * self.blocks_per_disk

    def mapping(self, b):
        return b % NUM_DISKS, b // NUM_DISKS

    def write(self, b, data):
        self._check(b, data)
        d, off = self.mapping(b)
        self.disks[d].write_block(off, data, self.sync_each_write)

    def read(self, b):
        self._check(b)
        d, off = self.mapping(b)
        return self.disks[d].read_block(off)


# ---------------------------------------------------------------------------
# RAID 1: five-way mirroring. Logical block A lives at offset A on ALL five
# disks. A write updates all five copies; a read is served by one copy,
# chosen round-robin (disk = A mod N) to spread read load across mirrors.
# Usable capacity = one disk; tolerates up to 4 disk failures.
# ---------------------------------------------------------------------------
class RAID1(RAIDBase):
    level = "RAID1"

    @property
    def capacity_blocks(self):
        return self.blocks_per_disk

    def mapping(self, b):
        return b % NUM_DISKS, b

    def write(self, b, data):
        self._check(b, data)
        for d in self.disks:
            d.write_block(b, data, self.sync_each_write)

    def read(self, b):
        self._check(b)
        d, off = self.mapping(b)
        return self.disks[d].read_block(off)


# ---------------------------------------------------------------------------
# Parity RAID helpers shared by RAID 4 and RAID 5.
# A stripe has N-1 = 4 data blocks and 1 parity block.
# ---------------------------------------------------------------------------
class ParityRAID(RAIDBase):
    DATA_PER_STRIPE = NUM_DISKS - 1
    N_STRIPE_LOCKS = 1024

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Read-modify-write of a stripe must be atomic w.r.t. other writers of
        # the same stripe, otherwise concurrent updates would corrupt parity.
        self._stripe_locks = [threading.Lock() for _ in range(self.N_STRIPE_LOCKS)]

    @property
    def capacity_blocks(self):
        return self.DATA_PER_STRIPE * self.blocks_per_disk

    def parity_disk(self, stripe):
        raise NotImplementedError

    def data_disk(self, stripe, index):
        raise NotImplementedError

    def mapping(self, b):
        stripe, idx = divmod(b, self.DATA_PER_STRIPE)
        return self.data_disk(stripe, idx), stripe

    def write(self, b, data):
        """Small write using *subtractive parity* (read-modify-write):
            P_new = P_old XOR D_old XOR D_new
        Cost: 2 reads + 2 writes, independent of the number of disks."""
        self._check(b, data)
        stripe, idx = divmod(b, self.DATA_PER_STRIPE)
        dd = self.data_disk(stripe, idx)
        pd = self.parity_disk(stripe)
        with self._stripe_locks[stripe % self.N_STRIPE_LOCKS]:
            old_data = self.disks[dd].read_block(stripe)
            old_par = self.disks[pd].read_block(stripe)
            new_par = xor_bytes(old_par, old_data, data)
            self.disks[dd].write_block(stripe, data, self.sync_each_write)
            self.disks[pd].write_block(stripe, new_par, self.sync_each_write)

    def write_full_stripe(self, stripe, blocks):
        """Full-stripe write (optimisation for large sequential writes):
        parity is computed from the new data alone, so no reads are needed.
        Cost: N writes for N-1 blocks of data."""
        assert len(blocks) == self.DATA_PER_STRIPE
        for i, data in enumerate(blocks):
            self.disks[self.data_disk(stripe, i)].write_block(stripe, data, self.sync_each_write)
        pd = self.parity_disk(stripe)
        self.disks[pd].write_block(stripe, xor_bytes(*blocks), self.sync_each_write)

    def read(self, b):
        self._check(b)
        d, off = self.mapping(b)
        return self.disks[d].read_block(off)

    def verify_parity(self):
        """Check that every stripe XORs to zero. Returns number of bad stripes."""
        bad = 0
        for s in range(self.blocks_per_disk):
            if any(xor_bytes(*(d.read_block(s) for d in self.disks))):
                bad += 1
        return bad


# ---------------------------------------------------------------------------
# RAID 4: disks 0..3 hold data, disk 4 holds all parity.
#   stripe = A div 4, data disk = A mod 4, offset = stripe
# ---------------------------------------------------------------------------
class RAID4(ParityRAID):
    level = "RAID4"
    PARITY = NUM_DISKS - 1

    def parity_disk(self, stripe):
        return self.PARITY

    def data_disk(self, stripe, idx):
        return idx


# ---------------------------------------------------------------------------
# RAID 5: parity rotates across disks (left-symmetric, as in OSTEP Fig 38.7):
#   stripe 0:  0  1  2  3  P
#   stripe 1:  5  6  7  P  4
#   stripe 2: 10 11  P  8  9
#   stripe 3: 15  P 12 13 14
#   stripe 4:  P 16 17 18 19
# parity disk = (N-1) - (stripe mod N); data i goes to (pdisk + 1 + i) mod N.
# ---------------------------------------------------------------------------
class RAID5(ParityRAID):
    level = "RAID5"

    def parity_disk(self, stripe):
        return (NUM_DISKS - 1) - (stripe % NUM_DISKS)

    def data_disk(self, stripe, idx):
        return (self.parity_disk(stripe) + 1 + idx) % NUM_DISKS


LEVELS = {"RAID0": RAID0, "RAID1": RAID1, "RAID4": RAID4, "RAID5": RAID5}


def make_raid(level, directory, logical_bytes, block_size, sync_each_write=False):
    """Build an array of the given level whose capacity is (at least)
    `logical_bytes`. Disk size is derived from the level's efficiency, so
    every level stores the same logical data set."""
    cls = LEVELS[level]
    logical_blocks = -(-logical_bytes // block_size)
    per_disk = {
        "RAID0": -(-logical_blocks // NUM_DISKS),
        "RAID1": logical_blocks,
        "RAID4": -(-logical_blocks // (NUM_DISKS - 1)),
        "RAID5": -(-logical_blocks // (NUM_DISKS - 1)),
    }[level]
    return cls(directory, per_disk, block_size, sync_each_write)
