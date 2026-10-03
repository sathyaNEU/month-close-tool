# ADR-0005: read path uses typed query specs instead of SQL templates

Status: accepted, format settled by ADR-0009 (Stage 1b). Date: 2026-09-26. Amends D8 in the roadmap.

## Context

D8 planned the API read path (the queries behind match results, open items, exceptions and journals) as allowlisted SQL templates with named parameters and `IDENTIFIER(:t)`, running on Spark Connect locally and on a Databricks SQL warehouse in production. ADR-0002 shows that open-source Delta drops SQL parameter markers once its extension is loaded, so those templates cannot run on the local stack, and parameters are the one thing we cannot give up on this path, because filter values come from users.

## Decision

Read queries become typed query specs, which are small objects that name a table from the registry, the allowed columns, typed filters (equals, in, ranges and prefix match on allowlisted columns), sort order, keyset pagination and simple aggregates for subtotals. The query engine compiles a spec into DataFrame operations and runs it over Spark Connect, so user values only ever enter the plan as typed literals and never as SQL text, which makes injection impossible by construction and matches the rule that the engine builds Columns and never SQL strings. The query engine always adds an entity filter taken from the caller's identity, and a spec has no way to switch it off.

The production backend is chosen in Stage 10, either parameterised SQL generated from the same spec for a SQL warehouse or Databricks Connect with the same DataFrame backend, and whichever we pick has to agree with the local backend in a differential test. I did not choose to render user values into SQL as escaped literals, because that is exactly the injection risk the design exists to avoid, and I did not choose to move the local stack back to Spark 4.0, because the user chose Databricks Runtime 18 and there is no evidence that 4.0 behaves better.

## Consequences

Stage 1 spike S3 builds the first specs, measures their latency on Spark Connect against the plan's target (p95 of 1.5 s or less), and settles the spec format before the matching API is designed in Stage 5. Free-form SQL from clients was never allowed, so no API capability is lost, but some reporting needs may later want a richer spec, and each such change goes through a normal ADR.
