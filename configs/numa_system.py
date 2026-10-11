#!/usr/bin/env python3
"""
numa_system.py - build a NUMA machine inside gem5.

This is the simulated hardware for our reproduction of

    A. Muddukrishna, P. A. Jonsson, M. Brorsson,
    "Locality-Aware Task Scheduling and Data Distribution for OpenMP
     Programs on NUMA Systems and Manycore Processors",
    Scientific Programming, 2015.

WHAT WE ARE MODELLING
The paper's machine (Figure 1) is four AMD Opteron 6172 sockets giving
eight NUMA nodes and 48 cores. Figure 2 of the paper is the important
picture: reading 4 MB from node 0's own memory costs about 40 cycles,
reading it from a far node costs 240 to 340 cycles. That gap is the
entire reason the paper exists.

We build a smaller machine with the same shape:

      core 0        core 1        core 2        core 3
        |             |             |             |
      L1I/L1D       L1I/L1D       L1I/L1D       L1I/L1D
        |             |             |             |
       L2            L2            L2            L2
        \____________ \___________ /____________/
                   system crossbar
         |            |            |           |
      bank 0       bank 1       bank 2      bank 3
      30 ns        90 ns       150 ns      210 ns
     (near)                                 (far)

Each bank is a separate memory controller covering its own slice of the
physical address space, with its own access latency. The latencies are
scaled from Figure 2 of the paper, keeping the same near-to-far ratio.

HONEST LIMITATION, SAY THIS OUT LOUD IN THE PRESENTATION
gem5's classic memory system gives each memory controller a single
latency, so what we model is distance measured from one node, exactly
like Figure 2 of the paper measures everything from node 0. We are not
modelling a full node-to-node distance matrix. The cause and the effect
we are demonstrating are the same ones the paper demonstrates: a core
that is served by a far bank stalls longer and the program takes longer.

Usage:
  gem5.opt numa_system.py --binary ./map_numa --options "local 4 4 32 2048 2"
"""

import argparse

import m5
from m5.objects import *

# --------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------
parser = argparse.ArgumentParser(description="NUMA machine in gem5")
parser.add_argument("--binary", required=True, help="static SE-mode binary")
parser.add_argument("--options", default="", help="argv for the binary")
parser.add_argument("--cores", type=int, default=4)
parser.add_argument("--banks", type=int, default=4)
parser.add_argument("--bank-size", default="32MB")
parser.add_argument(
    "--latencies",
    default="30ns,90ns,150ns,210ns",
    help="access latency of each memory bank, near to far",
)
parser.add_argument("--clock", default="2GHz")
parser.add_argument("--l1d-size", default="32kB")
parser.add_argument("--l1i-size", default="32kB")
parser.add_argument("--l2-size", default="256kB")
parser.add_argument("--max-insts", type=int, default=0)
args = parser.parse_args()

bank_latencies = [s.strip() for s in args.latencies.split(",")]
if len(bank_latencies) != args.banks:
    raise SystemExit(
        f"got {len(bank_latencies)} latencies for {args.banks} banks"
    )


# --------------------------------------------------------------------
# Cache definitions
# --------------------------------------------------------------------
class L1Cache(Cache):
    assoc = 8
    tag_latency = 2
    data_latency = 2
    response_latency = 2
    mshrs = 16
    tgts_per_mshr = 20


class L1ICache(L1Cache):
    size = args.l1i_size


class L1DCache(L1Cache):
    size = args.l1d_size


class L2Cache(Cache):
    size = args.l2_size
    assoc = 16
    tag_latency = 12
    data_latency = 12
    response_latency = 12
    mshrs = 20
    tgts_per_mshr = 12


# --------------------------------------------------------------------
# System
# --------------------------------------------------------------------
system = System()
system.clk_domain = SrcClockDomain()
system.clk_domain.clock = args.clock
system.clk_domain.voltage_domain = VoltageDomain()
system.mem_mode = "timing"
system.cache_line_size = 64

# ---- Address layout of the NUMA banks -------------------------------
#
# We want bank i to own the contiguous block [i*bank, (i+1)*bank).
#
# The obvious way is four separate AddrRanges. That does not work, and
# the reason is worth knowing: gem5's syscall-emulation mode builds one
# physical page pool per contiguous memory range, and a process always
# allocates out of pool 0. So with four plain ranges the program can only
# ever get pages from bank 0, and no data distribution policy is possible.
#
# The fix is to declare the banks as an *interleaved* range whose
# interleaving bits are the top address bits. gem5 merges interleaved
# ranges into a single pool, so the whole memory is allocatable, while
# the address decode still sends block i to bank i.
#
# With bank_size = 32MB = 2^25 and 4 banks, we interleave on bits 26 and
# 25, which makes the bank index exactly (addr >> 25) & 3. That is just
# "which 32MB block is this", which is what we wanted all along.
bank_bytes = int(Addr(args.bank_size))
total_bytes = bank_bytes * args.banks

intlv_bits = args.banks.bit_length() - 1
if 1 << intlv_bits != args.banks:
    raise SystemExit("--banks must be a power of two")
if bank_bytes & (bank_bytes - 1):
    raise SystemExit("--bank-size must be a power of two")

bank_shift = bank_bytes.bit_length() - 1          # 25 for 32MB
intlv_high_bit = bank_shift + intlv_bits - 1      # 26 for 4 banks

system.mem_ranges = [
    AddrRange(
        start=0,
        size=total_bytes,
        intlvHighBit=intlv_high_bit,
        intlvBits=intlv_bits,
        intlvMatch=i,
    )
    for i in range(args.banks)
]

print("NUMA map of the simulated machine")
print(f"  total physical memory: {total_bytes >> 20} MB in {args.banks} banks")
for i in range(args.banks):
    tag = "near" if i == 0 else ("far" if i == args.banks - 1 else "mid")
    lo = i * bank_bytes
    hi = lo + bank_bytes
    print(f"  bank {i}: 0x{lo:09x} - 0x{hi:09x} "
          f"({bank_bytes >> 20} MB)  latency {bank_latencies[i]:>6}  ({tag})")

# The crossbar every core reaches memory through.
system.membus = SystemXBar()

# Cores, each with private L1I, L1D and a private L2.
system.cpu = [X86TimingSimpleCPU(cpu_id=i) for i in range(args.cores)]

for cpu in system.cpu:
    cpu.icache = L1ICache()
    cpu.dcache = L1DCache()
    cpu.l2bus = L2XBar()
    cpu.l2cache = L2Cache()

    cpu.icache.cpu_side = cpu.icache_port
    cpu.dcache.cpu_side = cpu.dcache_port
    cpu.icache.mem_side = cpu.l2bus.cpu_side_ports
    cpu.dcache.mem_side = cpu.l2bus.cpu_side_ports
    cpu.l2cache.cpu_side = cpu.l2bus.mem_side_ports
    cpu.l2cache.mem_side = system.membus.cpu_side_ports

    cpu.createInterruptController()
    cpu.interrupts[0].pio = system.membus.mem_side_ports
    cpu.interrupts[0].int_requestor = system.membus.cpu_side_ports
    cpu.interrupts[0].int_responder = system.membus.mem_side_ports

system.system_port = system.membus.cpu_side_ports

# One memory controller per NUMA bank. SimpleMemory lets us dial in the
# access latency directly, which is what we need to express NUMA distance.
system.mem_ctrls = [
    SimpleMemory(
        range=system.mem_ranges[i],
        latency=bank_latencies[i],
        latency_var="0ns",
        bandwidth="12.8GB/s",
    )
    for i in range(args.banks)
]
for ctrl in system.mem_ctrls:
    ctrl.port = system.membus.mem_side_ports

# --------------------------------------------------------------------
# Workload
# --------------------------------------------------------------------
system.workload = SEWorkload.init_compatible(args.binary)

process = Process()
process.cmd = [args.binary] + args.options.split()

# Every core runs the same process object. When the benchmark calls
# pthread_create, gem5 hands the new thread to an idle core.
for cpu in system.cpu:
    cpu.workload = process
    cpu.createThreads()

root = Root(full_system=False, system=system)
m5.instantiate()

if args.max_insts:
    for cpu in system.cpu:
        cpu.scheduleInstStop(0, args.max_insts, "max insts reached")

print("Simulation starting")
event = m5.simulate()
print(f"Exiting @ tick {m5.curTick()} because {event.getCause()}")
