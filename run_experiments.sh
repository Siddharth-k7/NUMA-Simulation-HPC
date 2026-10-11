#!/usr/bin/env bash
#
# run_experiments.sh - every simulation run for the project, in order.
#
# Experiment A: the paper's three data distribution policies, same kernel,
#               same amount of work, only the placement of the data changes.
# Experiment B: a NUMA distance sweep. Pin the working set to one bank at a
#               time and watch execution time climb. This is our version of
#               Figure 2 of the paper.
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
GEM5="${GEM5:-$HOME/gem5/build/X86/gem5.opt}"
CONFIG="$HERE/configs/numa_system.py"
BIN="$HERE/benchmarks/map_numa"
OUT="$HERE/results"

# Benchmark shape: 4 threads, 4 banks of 32 MB, 2 MB working set per
# thread (eight times the 256 kB L2, so every pass really goes to memory).
THREADS=4
BANKS=4
BANK_MB=32
SLICE_KB=2048
REPEATS=1

# Latency of each bank, near to far. Scaled from Figure 2 of the paper,
# where node 0 sees about 40 cycles locally and 240-340 cycles remotely.
LATENCIES="30ns,90ns,150ns,210ns"

mkdir -p "$OUT"

if [[ ! -x "$GEM5" ]]; then
  echo "gem5 binary not found at $GEM5" >&2
  echo "set GEM5=/path/to/gem5.opt" >&2
  exit 1
fi

run () {                       # run <name> <policy> [latency override]
  local name="$1" policy="$2" lat="${3:-$LATENCIES}"
  echo "=============================================================="
  echo " run: $name   (policy=$policy, bank latencies=$lat)"
  echo "=============================================================="
  "$GEM5" \
    --outdir="$OUT/$name" \
    "$CONFIG" \
      --binary "$BIN" \
      --options "$policy $THREADS $BANKS $BANK_MB $SLICE_KB $REPEATS" \
      --cores "$THREADS" \
      --banks "$BANKS" \
      --bank-size "${BANK_MB}MB" \
      --latencies "$lat" \
    2>&1 | tee "$OUT/$name/console.log" || true
}

# ---- Experiment A: the three policies --------------------------------
run locality_aware_coarse local
run work_stealing_remote  remote
run fine_interleaved      interleaved

# ---- Experiment B: NUMA distance sweep -------------------------------
# Keep the data in bank 0 every time and just change how far bank 0 is.
# Same program, same placement, only the distance changes, so whatever
# moves in the results is pure NUMA latency.
run dist_30ns  local "30ns,90ns,150ns,210ns"
run dist_90ns  local "90ns,90ns,150ns,210ns"
run dist_150ns local "150ns,90ns,150ns,210ns"
run dist_210ns local "210ns,90ns,150ns,210ns"

echo
echo "All runs finished. Stats are in $OUT/<run>/stats.txt"
echo "Now run:  python3 $HERE/analyze.py"
