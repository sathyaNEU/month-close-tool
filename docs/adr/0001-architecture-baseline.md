# ADR-0001: architecture baseline from the approved plan

Status: accepted. Date: 2026-09-26.

## Context

We are building an in-house month-close platform for a GOAT and StockX style resale marketplace, with transaction matching and journaling at the level of BlackLine, and the approved plan in `docs/plan/ROADMAP.md` records the full design. The plan was approved on 2026-09-26 with four answers from the user (Databricks Runtime 18 LTS with Unity Catalog, Temporal for orchestration, GitHub Actions for CI/CD, and permission to install the local toolchain with Homebrew). Everything is built stage by stage, and each stage ends with `make gate-N`, a written report, and the user's sign-off.

## Decision

The decisions table in the roadmap (D1 to D19) is the architecture baseline, and this ADR points to it rather than repeating it. The ones that shape all later work are Python 3.12 everywhere (D4), Postgres for configuration, workflow and audit with Delta for bulk data (D6), integer minor units for money with exact decimal FX math (D9), a typed rule DSL that compiles to Spark Columns and never to SQL strings (D10), and 128-bit deterministic hash IDs (D11).

When a later finding changes one of these decisions, we write a new ADR with the evidence and add a dated entry to the changelog at the end of the roadmap, so the approved text stays visible next to the amendment. The first amendments came out of Stage 0 itself (ADR-0002, ADR-0003 and ADR-0005).

## Consequences

Every stage can be checked against one written baseline, and a reviewer (or an auditor looking at program change control) can see exactly what changed after approval and why. The cost is a little ceremony for each change, which I think is the right trade for a system that will sit in the SOX scope once it replaces BlackLine.
