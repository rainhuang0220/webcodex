# WebCodex performance optimization benchmark

Baseline commit: `2466a3e80d7c90c4b6c80a58978281995a8f8b45`

Test host: Windows 11. Baseline and optimized builds used the same source baseline, machine, test project, and local loopback transport. Benchmark fixtures were read-only or used `apply_text_edits(dry_run=true)` so the test project was not mutated.

## Changes under test

1. `read_files` request planning
   - deduplicate identical ranges
   - merge overlapping and nearby ranges (`merge_gap=20`)
   - cap a merged range at 400 lines
   - preserve `expected_read_revision` fences
   - reconstruct original read results when the ordinary `read_files` API requires per-request output

2. Windows single-file native search fast-path
   - directly execute native `rg.exe` for eligible single-file searches
   - retain the existing generated Bash search as the fallback
   - fall back for directories, include/exclude globs, unavailable `rg`, unsafe/out-of-project targets, or truncated native output
   - literal patterns preserve canonical semantics because Runtime escapes literal text before producing the Runner search payload

3. `search_and_read`
   - one model-visible call performs bounded search and immediate source inspection
   - search matches are converted into read ranges
   - duplicate/overlapping/nearby ranges are coalesced before compound output
   - source reads keep canonical SHA/read-revision semantics and the existing read result budget
   - coding discovery recommends this tool when search is predictably followed by source inspection

## Microbenchmarks

### `read_files`

| Scenario | Baseline | Optimized | Improvement |
| --- | ---: | ---: | ---: |
| 8 duplicate ranges | 11.92 ms | 6.05 ms | 49.2% |
| 8 adjacent ranges | 9.09 ms | 6.66 ms | 26.8% |
| mixed two-file ranges | 8.64 ms | 6.74 ms | 22.0% |

Planner request-count examples:

- duplicate ranges: `8 -> 1`
- adjacent/nearby ranges: `8 -> 1`
- mixed ranges: `8 -> 5`

### Windows single-file search

20 runs per scenario, same project and query shape:

| Scenario | Baseline mean | Optimized mean | Improvement |
| --- | ---: | ---: | ---: |
| literal | 406.29 ms | 106.95 ms | 73.7% |
| regex | 393.17 ms | 106.86 ms | 72.8% |
| context ±20 | 392.13 ms | 108.81 ms | 72.3% |

Directory and glob searches remained on the fallback path in this original
benchmark and showed baseline-equivalent latency. The 2026-10-11 extension
below changes their current routing; these historical timings stay unchanged.

## Task-level benchmark

Fixture: real Vue source (`index.vue`) in the test project.

Task:

1. locate four occurrences of `进入商店`
2. inspect source around those matches
3. prepare the same exact edit for the first occurrence
4. run the edit as `dry_run=true`

20 alternating baseline/optimized runs were used to reduce ordering bias.

### Final result after compound-output coalescing

| Metric | Baseline | Optimized | Improvement |
| --- | ---: | ---: | ---: |
| total mean | 464.63 ms | 129.11 ms | 72.2% |
| total P50 | 468.63 ms | 119.46 ms | 74.5% |
| total P95 | 529.71 ms | 176.44 ms | 66.7% |
| inspect mean | 457.03 ms | 122.32 ms | 73.2% |
| edit dry-run mean | 6.02 ms | 5.77 ms | approximately unchanged |
| outer model-visible calls | 3 | 2 | 33.3% fewer |
| average returned bytes | 23.9 KB | 10.7 KB | 55.3% fewer |

All benchmark runs located the expected four matches and produced valid inspection/edit results.

For the four nearby matches, compound inspection reduced four requested source ranges to one unique returned range (`index.vue:6-136`), avoiding repeated source text while preserving all match locations.

## Interpretation

The largest local latency gain comes from avoiding the Windows Bash process chain for eligible single-file searches. `read_files` coalescing mainly reduces redundant Runner work. `search_and_read` is primarily a model-round-trip optimization: its local execution time is similar to performing search plus read sequentially, but it removes one outer model/tool turn and, after coalescing, significantly reduces duplicated source bytes sent to the model.

A cross-call read cache was intentionally not added. After the other optimizations, bounded local reads are already on the order of a few milliseconds, while cache invalidation must remain correct when editors, Git, scripts, or other agents modify files outside WebCodex.

## Validation performed

- formatting with `cargo fmt --all`
- focused `read_files` planner/coalescing tests
- focused `search_and_read` tests
- ToolDefinition/audit contract tests
- Adaptive MCP direct-surface tests
- coding-intent discovery test
- `cargo check -p webcodex`
- optimized dogfood builds
- real loopback A/B calls for search, compound inspection, fallback behavior, output budgeting, and task-level dry-run edit workflow

Warnings observed during validation were pre-existing unrelated warnings in the baseline tree; the optimization work introduced no known compile errors.

## Follow-up review: compound output and recovery

The Windows timings above are the contributor's original local measurements,
not new timings measured during the Linux review. They are not a claim that
end-to-end model response latency, directory searches, or Linux/macOS workflows
improve by the same percentage.

The follow-up review found that preserving member ranges for byte-ceiling
fallback had inadvertently re-expanded successful compound reads into duplicate
source blocks. The read core now retains the original member fallback while
returning each successful union once for `search_and_read`. Ordinary `read_files`
still preserves the caller's per-item ranges and order.

The compound read phase now also applies canonical actionable continuation and
sparse projection. `reads.suggested_call` carries the exact resolved Project,
explicit business Session, and observed read revision for a continued range.
Output indexes and `coalesced_read_count` follow the actual ranges, including
member ranges used after a merged read exceeds the byte ceiling, rather than
reporting an optimistic pre-execution plan.

Compound inspection also has an explicit ToolDefinition-owned exploration
projection for its nested search result. Session records retain sanitized,
deduplicated matched paths for handoff instead of treating the compound output
as an ordinary `items`-based search batch and silently losing those paths.

Runner-boundary regression fixtures exercise:

- Four overlapping matches produce one `6..136` source block, with all four
  match positions preserved.
- Read-budget truncation provides a parser-ready continuation; changing the file
  before that continuation rejects the stale revision without returning new text.
- A merged range that exceeds the raw-byte ceiling falls back to the original
  independently valid ranges and retains correct continuation indexes.
- Removing duplicate members does not drop a later deferred coalesced group.
- No matches finish without a file read.

Run the focused cross-platform checks with:

```bash
cargo test --locked -p webcodex --lib search_and_read
cargo test --locked -p webcodex --lib read_files
```

These fixtures validate correctness and the `4 -> 1` source-block reduction,
not native Windows process execution or the original latency percentages.
Native Windows execution and an exact-head Windows A/B benchmark remain separate
validation evidence.

## Native Windows verification during PR #526 review

The follow-up was exercised on Windows 11 (build 26100), using the
`x86_64-pc-windows-msvc` toolchain and an installed native `rg.exe`. The isolated
real-process fixture invokes the production native search helper, rather than
mocking its stdout. It covers a Unicode filename containing spaces, CRLF input,
match/file/count result modes, no-match exit status, and fallback selection for
directories, missing files, out-of-project paths, and include globs.

This exposed a single-file count defect: `rg --count --null` emits only the count
when given one file. The canonical parser requires a filename-bearing record.
Both native Windows and generated fallback commands now request
`--with-filename`; the regression verifies the exact `path + NUL + count` record.
The fixture failed before the fix and passed afterward. An ordinary argv test
also protects the flag without requiring real-process execution.

Run native verification explicitly:

```text
cargo test --locked -p webcodex-runner --features runner-real-process-tests runner_real_process_native_search_preserves_modes_and_fallback -- --ignored --test-threads=1 --nocapture
```

This closes the native correctness gap for the scenarios listed above. It does
not remeasure the contributor's 20-run A/B workload, validate every timeout or
cancellation path, or establish an end-to-end model latency improvement. The
original timing percentages remain separately attributed measurements.

## 2026-10-11: native directory/glob search and compact discovery

The native Windows rg path now handles eligible project-relative directories
and include/exclude globs, with the same mandatory exclusions as the generated
POSIX backend. Shared core constants keep those exclusions and output budgets
canonical. User glob precedence, literal-pattern escaping, all three result
modes, context, marker framing and canonical parsing are preserved.

The stdout reader retains the same global line prefix and 32 KiB + one-byte
probe as the head pipeline, closes the pipe at the bound, and uses the existing
managed process-tree cleanup. It does not collect a complete directory scan
before truncating. Known rg errors, timeout, cancellation, uncertain cleanup,
credential isolation, allowed cwd roots and fallback remain explicit. Native
execution also checks allow_raw_shell and clamps the requested timeout to the
same local policy used by internal POSIX execution.

MCP compact tools/list now selects authorized Direct/extension/App descriptors
before constructing input schemas and skips discarded output schemas. Full
schema mode retains canonical outputs and adapter overlays. No tool admission,
OAuth scope, App visibility, resource metadata, input bound or execution
permission is widened. The existing canonical input cache remains unchanged;
this is not a claim that cold initialization derives only selected enum variants.

Pre-integration focused validation passed 94 tests: 27 Runner search/process/Job
lifecycle tests (including the two explicitly enabled native rg fixtures),
14 MCP list checks, 10 compact projection checks, 42 search/parser/model-surface/App
checks, and one descriptor/full-contract parity test. The new 12-case auth/protocol/Apps
matrix proves zero canonical output-schema materializations in compact mode
and one input materialization for each selected canonical tool. Full/compact
schema parity, discovery order and metadata are also checked. Formatting and
whitespace checks are separate final gates; no service deployment is included.

The small recursive fixture includes Unicode/spaces, CRLF, protected paths,
user glob exclusions, context, global result limits, a long UTF-8 line, missing
rg, cancellation and local policy refusal. Native and Git Bash/head records
agree in matches/files_with_matches/count modes after path-separator and order
normalization. The first comparison fixture incorrectly supplied a native-only
path separator through MSYS and had a mismatched long-line pattern; correcting
those fixture inputs resolved both assertions without weakening production
limits or contracts.

On this Windows host, the final single observations were:

| Recursive fixture mode | Native rg | Git Bash/head |
|---|---:|---:|
| matches | 87 ms | 236 ms |
| files_with_matches | 87 ms | 185 ms |
| count | 82 ms | 235 ms |

These initial smoke observations are separate from the repeated A/B measurement
below and do not establish end-to-end ChatGPT latency.

### Repeated Windows A/B measurement

The measured baseline is commit `805fc51d2c61172532d4bd95d3cf9b5bc90dc181`;
the optimized side is checkpoint `98e58fa66825484a3228c4b99d1398bf7b10f7c3`,
before integration with newer upstream commits. Both used isolated source
snapshots and the `dogfood` profile on Windows 11 build 26200, an Intel Core
i7-9750H (12 logical processors), 31.8 GiB RAM, rustc 1.96.0 and ripgrep 15.2.0.
Source fingerprints, fresh package compilation and distinct baseline/optimized
executable hashes were checked before sampling. A detected Cargo snapshot cache
reuse was invalidated and both variants rebuilt before any reported sample was taken.

Each case/variant had 10 warmups followed by 100 measured runs. Baseline and
optimized requests alternated AB/BA within each case. Mean is the arithmetic
mean; P95 uses nearest rank, `sorted[ceil(0.95 * N) - 1]`. There are 800 measured
samples in the published [raw timing CSV](benchmarks/windows-search-compact-tools-list-2026-10-11.csv).
Only workload labels, variant, iteration, sequence and nanosecond timings are
published; generated binaries and machine-local logs are excluded.

| Case | Baseline mean | Optimized mean | Mean reduction | Baseline P95 | Optimized P95 | P95 reduction |
|---|---:|---:|---:|---:|---:|---:|
| Windows directory search | 393.03 ms | 93.22 ms | 76.28% | 454.38 ms | 96.12 ms | 78.85% |
| Windows include/exclude glob search | 395.30 ms | 95.28 ms | 75.90% | 424.39 ms | 101.01 ms | 76.20% |
| Compact tools/list, Apps disabled | 642.83 ms | 34.66 ms | 94.61% | 814.59 ms | 47.01 ms | 94.23% |
| Compact tools/list, Apps enabled | 365.08 ms | 21.15 ms | 94.21% | 639.08 ms | 34.50 ms | 94.60% |

The search fixture has 512 ordinary files (9,215,464 bytes), 200 CRLF lines per
file, Unicode/spaces in paths, and six additional synthetic protected files.
Directory search returns eight matches; `**/*.rs` with `**/skip/**` excluded
returns seven. Both use `needle\.literal`, `path=src`, `limit=50`, zero context
and a 30-second timeout. Timing includes native/fallback route selection, process
startup, bounded collection, process-tree cleanup and Runner normalization.
Record sorting/path normalization for equivalence checks is outside the timer.
Every baseline search selected Git Bash/head; every optimized search selected
native rg. Normalized records and hashes matched, including the exclusion of
all synthetic protected files, and the fixture was unchanged after the run.

Compact timing includes the production `handle_list` handler, JSON serialization
and response DOM destruction, using a synthetic Bootstrap admin and the
stateless-2026 protocol. HTTP transport, network, audit dispatch, model latency
and compilation are outside the timer. Full serialized responses matched byte
for byte in each pair: 37 tools / 83,856 bytes with Apps disabled and 59 tools /
107,794 bytes with Apps enabled.

| Compact case | Canonical input materializations, before -> after | Canonical output materializations, before -> after |
|---|---:|---:|
| Apps disabled | 2,274 -> 35 | 2,274 -> 0 |
| Apps enabled | 2,298 -> 55 | 2,298 -> 0 |

These counters cover canonical registry schemas; adapter-local gateway/work
schemas are outside the counters. They explain avoided construction and cloning,
not the response size, which is unchanged. The two Apps cases ran in separate
cohorts; their absolute times must not be interpreted as enabling Apps making
discovery faster. Results describe warmed local paths on this host, not cold
initialization, arbitrary projects, production P95 or end-to-end ChatGPT speed.
After upstream integration, focused correctness checks are rerun; these timings
remain attributed to the original measured checkpoint rather than the newer
integrated PR head.

The integrated upstream baseline is `d87a9632`, which already includes PR #1034's
immutable schema cache and schema-free routing checks. The percentages above
are relative to `805fc51d`, not an incremental A/B measurement against that newer
main. Integration preserves both upstream improvements.

### Validation after upstream integration

Focused checks on the integrated Rust source passed 92 tests:

| Check | Passed |
|---|---:|
| Descriptor/full-contract parity | 1 |
| Runner native-search checks | 6 |
| Explicit real-process native rg/fallback fixtures | 2 |
| MCP tools/list | 14 |
| MCP compact projection | 10 |
| Model surface and routing | 31 |
| Project search command/parser | 24 |
| Agent and Job continuation App discovery | 2 |
| Opt-in execution-control schema | 1 |
| Schema-free operator-extension route lookup | 1 |

An initial local compile selected a stale core artifact and reported the missing
`project_search` export even though that module existed in the current source.
The changed Rust files' mtimes were refreshed without changing their bytes;
Cargo then explicitly recompiled core and its dependents, and every check above
passed. This is separate from the pre-sampling cache invalidation recorded above.
Formatting, whitespace and repository-local Markdown links also passed. These
checks do not include a full workspace/E2E run, deployment, Linux/macOS execution
or a new performance measurement of the integrated head.
