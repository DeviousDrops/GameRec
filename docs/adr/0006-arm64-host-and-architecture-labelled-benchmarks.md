# ARM64 host, and benchmarks labelled by architecture

GameRec runs on an Oracle Cloud Ampere A1 (arm64), so every image is built multi-arch. MinDB's int8
cascade is accelerated by an AVX2/FMA kernel that exists only on x86; on ARM it falls back to pure Go.
The deployed service is therefore measurably slower than any x86 benchmark of the same code, and the
project reports x86 and arm64 figures as separate, labelled results rather than one number.

## Consequences

Latency work must be measured on the target architecture — an x86 development machine will flatter
every result. Quoting an x86 figure for an ARM deployment would be the easiest dishonest thing in this
project, which is precisely why the labelling rule is written down rather than left to judgement.
