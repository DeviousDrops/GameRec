# ARM64 host, and benchmarks labelled by architecture

GameRec runs on an Oracle Cloud Ampere A1 (arm64), so every image is built multi-arch. MinDB's int8
cascade is accelerated by an AVX2/FMA kernel that exists only on x86; on ARM it falls back to pure Go.
The deployed service is therefore measurably slower than any x86 benchmark of the same code, and the
project reports x86 and arm64 figures as separate, labelled results rather than one number.

## Consequences

Latency work must be measured on the target architecture — an x86 development machine will flatter
every result. Quoting an x86 figure for an ARM deployment would be the easiest dishonest thing in this
project, which is precisely why the labelling rule is written down rather than left to judgement.

## Revised 2026-09-26: the host is x86_64

The deployment host is an Azure `Standard_B2als_v2` (x86_64, 2 vCPU, 4 GiB), not an Ampere A1. The
premise above is therefore no longer true, and two of its consequences change:

- MinDB selects its **AVX2 kernel** in production, so the x86 tables in `bench/README.md` describe the
  deployed architecture rather than a flattering approximation of it. They were measured on a 20-thread
  i7-13700H and production is 2 burstable vCPU, so the *kernel* label now matches and the CPU does not
  — which is the same trap one level down, and the tables say so.
- Images are built for `linux/amd64` only (D45). MinDB stays multi-arch because it is Go.

**The labelling rule itself stands unchanged**, and is the reason this revision is short: every number
already carries the kernel `/health` reported, so a host change invalidates a premise here without
invalidating any published figure. See D46.
