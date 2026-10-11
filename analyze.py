#!/usr/bin/env python3
"""
analyze.py - turn gem5's stats.txt files into the numbers and the chart
we actually show in the presentation.

What we pull out, and why each one matters for the paper:

  simSeconds            how long the program took. This is the paper's
                        "execution time" in Figures 3, 7 and 9.

  CPI                   cycles per instruction. The cores run the exact
                        same instructions in every policy, so any rise in
                        CPI is the core sitting idle waiting for memory.
                        This is our version of the paper's "dispatch stall
                        cycles".

  L2 miss latency       average cycles to service a miss that went out to
                        memory. This is the paper's Figure 2 and Figure 5.

  accesses per bank     proof that the data distribution policy actually
                        moved the data where we said it did.
"""

import os
import re
import sys
import json

HERE = os.path.dirname(os.path.realpath(__file__))
RESULTS = os.path.join(HERE, "results")

RUNS_A = [
    ("work_stealing_remote", "Work-stealing, data on a far node (baseline)"),
    ("fine_interleaved", "Fine distribution, pages striped over banks"),
    ("locality_aware_coarse", "Locality-aware + coarse, data on near node"),
]

RUNS_B = [
    ("dist_30ns", 30),
    ("dist_90ns", 90),
    ("dist_150ns", 150),
    ("dist_210ns", 210),
]


def read_stats(run):
    """
    Parse a gem5 stats.txt into {name: float}.

    The benchmark calls m5_reset_stats before the parallel region and
    m5_dump_stats after it, so the FIRST block in stats.txt covers
    exactly the parallel region. gem5 writes a second block when the
    program exits, which also includes teardown, and we ignore it.
    """
    path = os.path.join(RESULTS, run, "stats.txt")
    if not os.path.exists(path):
        return None
    stats = {}
    blocks = 0
    with open(path) as fh:
        for line in fh:
            if "Begin Simulation Statistics" in line:
                blocks += 1
                if blocks > 1:
                    break
                continue
            line = line.split("#")[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                stats[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return stats


def summarise(stats):
    """Collapse per-core and per-bank stats into the handful we report."""
    out = {}
    out["sim_seconds"] = stats.get("simSeconds", float("nan"))

    cycles = sum(v for k, v in stats.items()
                 if re.fullmatch(r"system\.cpu\d*\.numCycles", k))
    insts = sum(v for k, v in stats.items()
                if re.fullmatch(r"system\.cpu\d*\.commitStats\d+\.numInsts", k))
    out["cycles"] = cycles
    out["instructions"] = insts
    out["cpi"] = cycles / insts if insts else float("nan")

    # Average cost of an L2 miss, weighted over all cores.
    lat_total, miss_total = 0.0, 0.0
    for k, v in stats.items():
        m = re.fullmatch(r"system\.cpu(\d*)\.l2cache\.overallMissLatency::total", k)
        if m:
            lat_total += v
            miss_total += stats.get(
                f"system.cpu{m.group(1)}.l2cache.overallMisses::total", 0.0)
    out["l2_misses"] = miss_total
    out["l2_miss_latency_ticks"] = lat_total / miss_total if miss_total else 0.0

    # Where did the memory traffic actually land?
    banks = {}
    for k, v in stats.items():
        m = re.fullmatch(r"system\.mem_ctrls(\d+)\.numReads::total", k)
        if m:
            idx = int(m.group(1))
            banks[idx] = banks.get(idx, 0.0) + v
        m = re.fullmatch(r"system\.mem_ctrls(\d+)\.numWrites::total", k)
        if m:
            idx = int(m.group(1))
            banks[idx] = banks.get(idx, 0.0) + v
    out["bank_accesses"] = [banks.get(i, 0.0) for i in range(max(banks) + 1)] \
        if banks else []
    return out


def fmt_bank_split(accesses):
    total = sum(accesses) or 1.0
    return "  ".join(
        f"b{i}:{100.0 * a / total:5.1f}%" for i, a in enumerate(accesses))


def main():
    collected = {}
    for run, _ in RUNS_A:
        s = read_stats(run)
        if s:
            collected[run] = summarise(s)
    for run, _ in RUNS_B:
        s = read_stats(run)
        if s:
            collected[run] = summarise(s)

    if not collected:
        print(f"no stats found under {RESULTS}, run run_experiments.sh first")
        return 1

    lines = []
    add = lines.append

    add("=" * 78)
    add("EXPERIMENT A: the paper's data distribution and scheduling policies")
    add("=" * 78)
    add("")
    add("Same kernel, same number of instructions in every row.")
    add("The only difference is which memory bank serves the data.")
    add("")
    header = (f"{"policy":<48}{'time (ms)':>11}{'CPI':>8}"
              f"{'speedup':>9}")
    add(header)
    add("-" * len(header))

    base = collected.get("work_stealing_remote", {}).get("sim_seconds")
    for run, label in RUNS_A:
        if run not in collected:
            continue
        c = collected[run]
        sp = base / c["sim_seconds"] if base and c["sim_seconds"] else float("nan")
        add(f"{label:<48}{c['sim_seconds'] * 1e3:>11.3f}"
            f"{c['cpi']:>8.2f}{sp:>8.2f}x")
    add("")
    add("Speedup is measured against the work-stealing row, which is the"
        " baseline the paper uses.")
    add("")

    add("Where the memory traffic actually went (proof the placement worked):")
    add("")
    for run, label in RUNS_A:
        if run not in collected:
            continue
        add(f"  {label:<48}{fmt_bank_split(collected[run]['bank_accesses'])}")
    add("")

    add("Average cost of one L2 miss, in ticks (1 tick = 1 ps):")
    add("")
    for run, label in RUNS_A:
        if run not in collected:
            continue
        c = collected[run]
        add(f"  {label:<48}{c['l2_miss_latency_ticks']:>12,.0f}"
            f"   ({c['l2_misses']:,.0f} misses)")
    add("")

    if any(r in collected for r, _ in RUNS_B):
        add("=" * 78)
        add("EXPERIMENT B: NUMA distance sweep (our version of Figure 2)")
        add("=" * 78)
        add("")
        add("Identical program, identical data placement. We only change how"
            " far away")
        add("the bank holding the data is. Everything that moves here is"
            " pure NUMA cost.")
        add("")
        h2 = f"{'bank latency':>14}{'time (ms)':>12}{'CPI':>8}{'slowdown':>11}"
        add(h2)
        add("-" * len(h2))
        near = collected.get("dist_30ns", {}).get("sim_seconds")
        for run, ns in RUNS_B:
            if run not in collected:
                continue
            c = collected[run]
            sd = c["sim_seconds"] / near if near else float("nan")
            add(f"{str(ns) + ' ns':>14}{c['sim_seconds'] * 1e3:>12.3f}"
                f"{c['cpi']:>8.2f}{sd:>10.2f}x")
        add("")

    text = "\n".join(lines)
    print(text)

    with open(os.path.join(RESULTS, "summary.txt"), "w") as fh:
        fh.write(text + "\n")
    with open(os.path.join(RESULTS, "summary.json"), "w") as fh:
        json.dump(collected, fh, indent=2)
    print(f"\nwritten: {RESULTS}/summary.txt and summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
