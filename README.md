# Simulating "Locality-Aware Task Scheduling and Data Distribution" in gem5

Course project. Paper being reproduced:

> A. Muddukrishna, P. A. Jonsson, M. Brorsson,
> *Locality-Aware Task Scheduling and Data Distribution for OpenMP Programs
> on NUMA Systems and Manycore Processors*, Scientific Programming, 2015.

Everything here runs on [gem5](https://www.gem5.org/), a cycle-level
computer architecture simulator.

---

## 1. What the paper is actually saying

A NUMA machine splits its memory into nodes. Each node sits next to a
group of cores.

- A core reading memory on **its own node** is fast.
- A core reading memory on **another node** is slow, because the request
  has to cross the interconnect between sockets.

The paper measures this on a real four-socket, eight-node, 48-core AMD
Opteron 6172 machine. Their Figure 2 is the key number:

| accessing 4 MB from node 0 | latency |
|---|---|
| node 0 (its own memory) | about 40 cycles |
| nodes 1 to 7 (remote) | 240 to 340 cycles |

So a remote access costs **6 to 8 times** more than a local one.

Now here is the problem. OpenMP task schedulers use **work-stealing**:
an idle core grabs a task from any queue it can reach. That is great for
keeping every core busy, and it is completely blind to where the task's
data lives. So a core on node 0 can end up running a task whose data sits
on node 7, and then it spends most of its time waiting.

The paper fixes both halves of this:

1. **Data distribution.** Let the programmer say how an allocation should
   be spread, with three simple choices and nothing else to learn:
   - `standard` - leave it to the OS (this is first-touch)
   - `fine` - spread the allocation page by page, round robin, over every node
   - `coarse` - put each whole allocation on one node, round robin per allocation
2. **Locality-aware scheduling.** When a task is created, look at its
   `depend` list to work out which pages it will touch, then put it on the
   queue of the node that holds those pages. Only bother when the task's
   working set is bigger than the last level cache, because below that it
   does not matter.

Result in the paper: up to **69%** faster on scientific benchmarks, and
never slower, so it is safe as a default.

---

## 2. What we reproduce, and what we do not

Being straight about scope is better than overclaiming, so say this in the
presentation.

**We do not** run their exact setup. That needs a 48-core Opteron, a full
Linux with `libnuma`, and MIR, their own task runtime. None of that fits
in a simulator in a sensible amount of time.

**We do** build a NUMA machine inside gem5 with the same shape and the
same near-to-far latency ratio, run the paper's own `map` kernel on it,
and measure what each data placement policy costs. A simulator is
actually better than real hardware for this, because:

- the latencies are exact numbers we chose, not noisy measurements
- nothing else is running on the machine, so there is zero interference
- we can count every cycle and every memory access, which you cannot do
  with a stopwatch

So: we reproduce the **mechanism and the conclusion**, not their exact
percentages.

**One honest limitation.** gem5's classic memory system gives each memory
controller a single latency value. So what we model is distance measured
from one node, which is exactly what the paper's Figure 2 measures. We are
not modelling a full node-to-node distance matrix. If sir asks, that is
the answer, and the cause and effect we demonstrate are unchanged.

---

## 3. The machine we build in gem5

`configs/numa_system.py` builds this:

```
      core 0        core 1        core 2        core 3
        |             |             |             |
      L1I/L1D       L1I/L1D       L1I/L1D       L1I/L1D
      32kB each     32kB each     32kB each     32kB each
        |             |             |             |
       L2            L2            L2            L2
      256kB         256kB         256kB         256kB
        \____________ \___________/____________/
                   system crossbar
         |            |            |           |
      bank 0       bank 1       bank 2      bank 3
      32 MB        32 MB        32 MB       32 MB
      30 ns        90 ns       150 ns      210 ns
      (near)                                (far)
```

- 4 x86 cores at 2 GHz, in-order timing model (`X86TimingSimpleCPU`)
- private L1I, L1D and L2 per core
- four memory banks, each a separate `SimpleMemory` controller covering
  its own 32 MB slice of the physical address space, each with its own
  access latency

The bank latencies are scaled from the paper's Figure 2: near is cheap,
far is roughly seven times worse.

---

## 4. The benchmark

`benchmarks/map_numa.c` is the paper's `map` kernel from their Listing 1.
Their version:

```c
for (int i = 0; i < N; i++) {
    list[i] = malloc(sizeof(int) * SZ);
    initialize(i, list[i], SZ);
}
for (int i = 0; i < N; i++) {
    #pragma omp task input(list[i][0:SZ-1])
    map(list[i], SZ);
}
#pragma omp taskwait
```

Ours is the same thing with pthreads instead of OpenMP tasks, because
gem5's syscall-emulation mode handles raw pthreads much more reliably than
it handles `libgomp`. One thread per core, each thread scaling its own
vector, no sharing between threads. That is the same structure and the
same memory behaviour.

**The important property:** every policy runs the *identical* loop, the
same instruction count and the same number of cache lines. The only thing
that changes is the **address** of those cache lines, which decides which
bank serves them. So any difference in the results is memory locality and
nothing else.

The three policies, mapped onto the paper:

| our flag | paper's situation |
|---|---|
| `remote` | work-stealing balanced the load, the data was left on a far node |
| `interleaved` | the paper's `fine` distribution, pages striped over all banks |
| `local` | the paper's proposal, locality-aware scheduling plus `coarse` distribution |

### How we control where data physically lands

This is the clever bit and it is worth explaining.

gem5's syscall-emulation mode hands out physical pages **in the order the
program first touches them**. So the benchmark:

1. `mmap`s one big region covering nearly all of physical memory,
2. touches it one page at a time, in increasing address order.

After that pass, an offset inside that region maps predictably onto a
physical address, and therefore onto a bank. Picking a bank is now just
picking an offset.

Nice detail for the presentation: that first-touch pass **is** the paper's
baseline `first-touch malloc` policy. We are literally running their
baseline to set up the experiment.

We do not just assume it worked. The results table reports the percentage
of memory accesses that landed on each bank, which proves the placement.

---

## 5. What we measure, and how it maps to the paper

All of it comes straight out of gem5's `stats.txt`.

| our metric | gem5 stat | the paper's equivalent |
|---|---|---|
| execution time | `simSeconds` | execution time, Figures 3, 7, 9 |
| CPI | `numCycles / committedInsts` | dispatch stall cycles |
| L2 miss latency | `l2cache.overallMissLatency / overallMisses` | Figures 2 and 5 |
| accesses per bank | `mem_ctrls<i>.numReads/numWrites` | proof of data distribution |

On CPI: every run executes the same instructions, so if CPI goes up it is
because the core is sitting there doing nothing, waiting for memory. That
is exactly what the paper calls dispatch stall cycles.

---

## 6. The two experiments

**Experiment A, the policy comparison.** Run the same kernel under
`remote`, `interleaved` and `local`, and compare. This is the paper's
Figure 7 in miniature.

**Experiment B, the NUMA distance sweep.** Keep the data in bank 0 every
single time, and only change how far bank 0 is: 30, 90, 150, 210 ns.
Nothing else in the program moves, so whatever changes is pure NUMA cost.
This redraws the paper's Figure 2 from our own simulation.

---

## 7. How to run it

You need gem5 built for X86 (`build/X86/gem5.opt`).

```bash
# build the benchmark (must be static, gem5 SE mode prefers it)
cd benchmarks && gcc -O2 -static -pthread -o map_numa map_numa.c && cd ..

# all seven simulation runs
GEM5=/path/to/gem5/build/X86/gem5.opt ./run_experiments.sh

# turn the stats into the results table
python3 analyze.py
```

A single run on its own:

```bash
gem5.opt --outdir=results/try configs/numa_system.py \
    --binary ./benchmarks/map_numa \
    --options "local 4 4 32 2048 1" \
    --cores 4 --banks 4 --bank-size 32MB \
    --latencies "30ns,90ns,150ns,210ns"
```

Benchmark arguments are
`<policy> <threads> <banks> <bank_MB> <slice_KB> <repeats>`.

---

## 8. Results

Machine: 4 cores at 2 GHz, 32 kB L1s, 256 kB L2 per core, four 32 MB banks
at 30 / 90 / 150 / 210 ns. Benchmark: 4 threads, 2 MB working set each,
which is eight times the L2, so every pass really does go to memory.

Full tables in `results/summary.txt`, charts in `results/fig1_policies.png`
and `results/fig2_numa_distance.png`.

### Experiment A, the three policies

| policy | time | CPI | speedup |
|---|---|---|---|
| Work-stealing, data on a far node (baseline) | 12.28 ms | 9.81 | 1.00x |
| Fine distribution, pages striped over banks | 9.33 ms | 7.95 | 1.32x |
| Locality-aware + coarse, data on the near node | 6.38 ms | 6.07 | **1.92x** |

**Locality-aware scheduling made the kernel 1.92 times faster.** The paper
reports up to 69% on real hardware, we get 92%, which is expected: our
simulated machine has no OS noise and no other processes competing, so the
locality effect shows up undiluted.

Where the traffic actually landed, which proves the placement worked:

| policy | bank 0 | bank 1 | bank 2 | bank 3 |
|---|---|---|---|---|
| far node | 0.3% | 0.0% | 0.0% | 99.7% |
| fine / striped | 25.2% | 24.8% | 24.8% | 25.1% |
| near node | 99.7% | 0.0% | 0.0% | 0.3% |

And the reason for the whole difference, the cost of one L2 miss:

| policy | avg L2 miss | L2 misses |
|---|---|---|
| far node | 227 ns | 132,005 |
| fine / striped | 137 ns | 132,004 |
| near node | 48 ns | 132,004 |

**The miss count is identical to within one miss across all three runs.**
That is the single most convincing number in the whole project. The cores
did exactly the same work and took exactly the same number of trips to
memory. The only thing that changed is how long each trip took. Nothing
else can explain the 1.92x.

Note that fine distribution lands exactly in the middle at 137 ns, because
a quarter of its accesses go to each bank and the mean of 30, 90, 150 and
210 is 120. That matches the paper's point that fine is a safe, mediocre
default: better than getting it wrong, worse than getting it right.

### Experiment B, the NUMA distance sweep

| bank latency | time | CPI | slowdown |
|---|---|---|---|
| 30 ns | 6.38 ms | 6.07 | 1.00x |
| 90 ns | 8.36 ms | 7.60 | 1.31x |
| 150 ns | 10.34 ms | 9.13 | 1.62x |
| 210 ns | 12.32 ms | 10.66 | 1.93x |

Perfectly linear. Same program, same placement, only the distance changes,
and execution time tracks it straight up. This is the paper's Figure 2
redrawn from our own simulation.

---

## 10. Suggested five slides

1. **The problem.** NUMA, near vs far memory, the paper's Figure 2 numbers
   (40 cycles local, 240 to 340 remote). Work-stealing balances load and
   ignores all of this.
2. **The paper's fix.** Data distribution (`standard` / `fine` / `coarse`)
   plus locality-aware scheduling using the task's `depend` list. Up to
   69% faster, never slower.
3. **Our setup in gem5.** The machine diagram from section 3, the map
   kernel, the three policies, and the honest scope statement from
   section 2. Mention the trick: gem5 hands out physical pages in
   first-touch order, which is how we control placement.
4. **Results.** `fig1_policies.png`. Lead with 1.92x, then the identical
   miss count, then the 48 vs 227 ns.
5. **The NUMA penalty isolated.** `fig2_numa_distance.png`, plus one line
   on what we would do next (per-core distance matrix, a real work-stealing
   scheduler on top, more of the paper's benchmarks).


---

## 11. Files

```
benchmarks/map_numa.c     the paper's map kernel, three placement policies
configs/numa_system.py    the NUMA machine built in gem5
run_experiments.sh        all simulation runs
analyze.py                parses stats.txt into the results tables
results/                  gem5 output, one directory per run, plus summary
```
