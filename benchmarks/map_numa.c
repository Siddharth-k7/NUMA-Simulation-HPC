/*
 * map_numa.c
 *
 * The "map" kernel from Listing 1 of:
 *   A. Muddukrishna, P. A. Jonsson, M. Brorsson,
 *   "Locality-Aware Task Scheduling and Data Distribution for OpenMP
 *    Programs on NUMA Systems and Manycore Processors",
 *   Scientific Programming, 2015.
 *
 * In the paper, N vectors are allocated, then N OpenMP tasks each scale
 * one vector. The whole point of the paper is that the SPEED of that
 * loop depends on which NUMA node each vector physically lives on,
 * and on whether the scheduler sends the task to a core near that node.
 *
 * We run the same kernel inside gem5 on a NUMA machine we build
 * ourselves, so we can measure exactly how many cycles get lost
 * waiting for remote memory.
 *
 * One pthread per core. Every thread does EXACTLY the same amount of
 * work (same instruction count, same number of cache lines touched).
 * The only thing the policy changes is WHICH memory bank serves those
 * cache lines:
 *
 *   local        every thread's working set is served by the near bank.
 *                = paper's locality-aware scheduling + coarse distribution
 *
 *   remote       every thread's working set is served by the far bank.
 *                = a load-balancing work-stealing scheduler that stole a
 *                  task away from the node holding its data
 *
 *   interleaved  each thread's pages are striped round-robin over all
 *                banks.
 *                = paper's "fine" data distribution
 *
 * HOW WE CONTROL PLACEMENT
 * gem5's syscall-emulation mode hands out physical pages in the order
 * they are first touched. So we mmap one big region that covers the
 * whole physical memory, touch it page by page in increasing order
 * once at startup, and from then on
 *      offset inside the region  ->  physical address  ->  memory bank
 * is a fixed, predictable mapping. That first-touch pass is itself the
 * paper's baseline "first-touch malloc" policy.
 *
 * Usage:
 *   map_numa <policy> <threads> <banks> <bank_MB> <slice_KB> <repeats>
 *   policy = local | remote | interleaved
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <pthread.h>
#include <sys/mman.h>
#include <unistd.h>

/*
 * gem5 instrumentation.
 *
 * The paper measures "execution time of the parallel section" and
 * ignores setup. We do the same: zero the simulator's counters right
 * before the threads start and dump them the moment they join. So the
 * numbers in results/ describe the parallel region and nothing else,
 * not our first-touch setup pass.
 *
 * Build with -DGEM5 and link against gem5's libm5 to enable this.
 */
#ifdef GEM5
#include <gem5/m5ops.h>
#define REGION_BEGIN() m5_reset_stats(0, 0)
#define REGION_END()   m5_dump_stats(0, 0)
#else
#define REGION_BEGIN() do {} while (0)
#define REGION_END()   do {} while (0)
#endif

#define PAGE_BYTES 4096u
#define WORDS_PER_PAGE (PAGE_BYTES / sizeof(uint32_t))

enum { POLICY_LOCAL = 0, POLICY_REMOTE = 1, POLICY_INTERLEAVED = 2 };

static uint8_t *pool;          /* the one big first-touched region */
static size_t   pool_bytes;
static size_t   bank_bytes;
static int      n_banks;
static int      n_threads;
static size_t   slice_bytes;   /* working set of ONE thread */
static int      repeats;
static int      policy;

typedef struct {
    int        tid;
    uint8_t  **pages;   /* the pages this thread will walk, in order */
    size_t     n_pages;
    uint64_t   checksum;
} targ_t;

/*
 * The kernel. Identical for every policy: same loop, same number of
 * loads and stores, same number of cache lines. Only the addresses in
 * pages[] differ, which is exactly the variable the paper studies.
 */
static void *worker(void *raw)
{
    targ_t *a = (targ_t *)raw;
    uint64_t sum = 0;

    for (int r = 0; r < repeats; r++) {
        for (size_t k = 0; k < a->n_pages; k++) {
            volatile uint32_t *v = (volatile uint32_t *)a->pages[k];
            for (size_t i = 0; i < WORDS_PER_PAGE; i++) {
                v[i] = v[i] * 3u + 1u;      /* the "map": scale a vector */
            }
            sum += v[0];
        }
    }
    a->checksum = sum;
    return NULL;
}

/*
 * Pick the physical home of page number `p` of thread `tid`.
 * Returns an offset inside `pool`.
 */
static size_t page_offset(int tid, size_t p, size_t pages_per_slice)
{
    size_t bank, within;

    switch (policy) {
    case POLICY_LOCAL:
        /* near bank, bank 0 */
        bank   = 0;
        within = (size_t)tid * slice_bytes + p * PAGE_BYTES;
        break;

    case POLICY_REMOTE:
        /* farthest bank */
        bank   = (size_t)(n_banks - 1);
        within = (size_t)tid * slice_bytes + p * PAGE_BYTES;
        break;

    case POLICY_INTERLEAVED:
    default:
        /* page p goes to bank (p % n_banks), round robin */
        bank   = p % (size_t)n_banks;
        within = (size_t)tid * slice_bytes + (p / (size_t)n_banks) * PAGE_BYTES;
        break;
    }

    (void)pages_per_slice;
    return bank * bank_bytes + within;
}

static int parse_policy(const char *s)
{
    if (!strcmp(s, "local"))       return POLICY_LOCAL;
    if (!strcmp(s, "remote"))      return POLICY_REMOTE;
    if (!strcmp(s, "interleaved")) return POLICY_INTERLEAVED;
    fprintf(stderr, "unknown policy '%s'\n", s);
    exit(2);
}

int main(int argc, char **argv)
{
    if (argc != 7) {
        fprintf(stderr,
          "usage: %s <local|remote|interleaved> <threads> <banks>"
          " <bank_MB> <slice_KB> <repeats>\n", argv[0]);
        return 2;
    }

    policy      = parse_policy(argv[1]);
    n_threads   = atoi(argv[2]);
    n_banks     = atoi(argv[3]);
    bank_bytes  = (size_t)atoi(argv[4]) * 1024u * 1024u;
    slice_bytes = (size_t)atoi(argv[5]) * 1024u;
    repeats     = atoi(argv[6]);

    pool_bytes = bank_bytes * (size_t)n_banks;
    size_t pages_per_slice = slice_bytes / PAGE_BYTES;

    /* Sanity: every thread's slice must fit inside one bank. */
    if ((size_t)n_threads * slice_bytes > bank_bytes) {
        fprintf(stderr, "threads * slice does not fit in one bank\n");
        return 2;
    }
    if (policy == POLICY_INTERLEAVED && pages_per_slice % (size_t)n_banks) {
        fprintf(stderr, "slice pages must divide evenly by banks\n");
        return 2;
    }

    printf("policy=%s threads=%d banks=%d bank=%zuMB slice=%zuKB repeats=%d\n",
           argv[1], n_threads, n_banks,
           bank_bytes >> 20, slice_bytes >> 10, repeats);

    /*
     * Reserve the whole physical memory as one region. We leave the last
     * few pages alone so the stack and libc still have somewhere to live.
     */
    size_t reserve = pool_bytes - (16u * 1024u * 1024u);
    pool = mmap(NULL, reserve, PROT_READ | PROT_WRITE,
                MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (pool == MAP_FAILED) { perror("mmap"); return 1; }
    pool_bytes = reserve;

    /*
     * FIRST TOUCH, in increasing address order.
     * After this, offset-inside-pool maps linearly onto physical address,
     * which is what lets us choose a memory bank by choosing an offset.
     */
    for (size_t off = 0; off < pool_bytes; off += PAGE_BYTES)
        pool[off] = (uint8_t)1;

    printf("first-touch done over %zu MB\n", pool_bytes >> 20);

    /* Build each thread's page list according to the policy. */
    pthread_t *tids = calloc((size_t)n_threads, sizeof(pthread_t));
    targ_t    *args = calloc((size_t)n_threads, sizeof(targ_t));

    for (int t = 0; t < n_threads; t++) {
        args[t].tid     = t;
        args[t].n_pages = pages_per_slice;
        args[t].pages   = calloc(pages_per_slice, sizeof(uint8_t *));
        for (size_t p = 0; p < pages_per_slice; p++) {
            size_t off = page_offset(t, p, pages_per_slice);
            if (off + PAGE_BYTES > pool_bytes) {
                fprintf(stderr, "offset past end of pool\n");
                return 1;
            }
            args[t].pages[p] = pool + off;
        }
    }

    printf("starting parallel region\n");
    fflush(stdout);

    REGION_BEGIN();                      /* simulator counters start here */
    for (int t = 1; t < n_threads; t++)
        pthread_create(&tids[t], NULL, worker, &args[t]);
    worker(&args[0]);                    /* thread 0 is the main thread */
    for (int t = 1; t < n_threads; t++)
        pthread_join(tids[t], NULL);
    REGION_END();                        /* and stop here */

    uint64_t total = 0;
    for (int t = 0; t < n_threads; t++) total += args[t].checksum;
    printf("parallel region done, checksum=%llu\n",
           (unsigned long long)total);
    return 0;
}
