# ADR-0011: the rule DSL and the configuration contracts

Status: proposed with Stage 2a. Date: 2026-09-27. Implements roadmap D10 and engineering rules 1 and 2.

## Context

Matching rules, data-source mappings and (from Stage 6) posting rules are configuration, not code. Roadmap D10 says they are a typed JSON DSL compiled to an IR and then to Spark Columns, never to SQL strings, with regex limited to an RE2-validated subset. Engineering rule 1 says configuration models are Pydantic models frozen as JSON Schema at each gate, and rule 2 says every configuration goes through draft, submitted, approved and active under maker-checker with an RFC 8785 content hash.

Three facts shaped the details:

1. The same expression runs in two engines: Spark (Stage 3's compiler) and a pure-Python interpreter (the reference matcher and every differential test). Wherever Spark and Python differ by default, one of them has to be told what the other does. Examples: `trim` (Spark strips spaces only), `substring` positions, `regexp_extract` returning an empty string on no match, two-digit years (`yy` is 2000 to 2099 in Spark), and decimal-to-string formatting.
2. Java's `java.util.regex` (inside Spark) and Python's `re` read some patterns differently: `.` versus `\r` and Unicode line terminators, `$` before a trailing terminator, `\d`/`\w` on non-ASCII digits and letters, named-group syntax, possessive quantifiers, and class intersection. Java also backtracks, so nested quantifiers can take exponential time on hostile input.
3. RFC 8785 canonical JSON only represents integers within ±(2^53 − 1). The round-trip property test found that a DSL literal above that range made a configuration impossible to hash.

## Decision

**Expressions** are JSON trees with three node kinds, `{"col": ...}`, `{"lit": ..., "type": ...}` and `{"fn": ..., "args": [...]}`. Types are `string`, `bigint` (minor units), `boolean`, `date`, `timestamp` (UTC) and `decimal` (DECIMAL(38,12)). Literal integers are limited to the JSON-safe range. Runtime arithmetic still uses the full BIGINT range and fails on overflow, as ANSI mode does.

**The function catalog** (`close_contracts.dsl.functions`, 46 functions) is the only thing an expression can call. Parameters that must be literals (regex patterns, date formats, calendar codes, group numbers) are validated before anything runs. Null handling follows SQL, with three-valued `and`/`or`.

**The typecheck** turns a tree plus column types into the IR and reports every problem at once, each with a stable code and a JSON path to the node. It limits expressions to 32 levels and 500 nodes. Nested `and`/`or` are flattened, and the IR has a canonical fingerprint.

**The interpreter** follows Spark's ANSI semantics wherever the two differ. A Spark test (`engine/tests/spark/test_dsl_spark_semantics.py`) checks those behaviours on real Spark instead of assuming them. Bad input (an unparseable amount, an impossible date, lost precision) raises an evaluation error, which a mapping treats as a rejected row.

**Regex patterns** are limited to a portable, linear-time subset, parsed by a small whitelist parser (`close_contracts.dsl.regex`):
- Allowed: literals, classes, the ASCII escapes, groups, alternation, bounded quantifiers, a leading `^` and a leading `(?i)`.
- Rejected, each with a code: backreferences, lookaround, named, atomic and flag groups, possessive quantifiers, `$`, POSIX and `\p` classes, class intersection, and any quantifier on a group that contains a quantifier or an alternation.
- Limits: at most four unbounded quantifiers and 256 characters.
- Python compiles the pattern with `re.ASCII`, and `.` is translated to Java's meaning (any character except a line terminator).
- The Spark test runs every accepted pattern of the corpus against Java on inputs chosen for the known differences.

**Configuration models** (`close_contracts.config`) cover:
- data sources: landing table, identity key, DSL mapping, filter, sign and incremental mode;
- match sets: source bindings, pass rules with typed conditions (exact keys, text, date windows, amount tolerances in integer arithmetic, sign, currency, and a general expression), limits, timing rules, owners and schedule;
- the accounting policy per program.

The validators enforce the roadmap's rules. An `M:M` rule needs an exact key. Every rule must be able to block. `1:M`/`M:1` rules need a `group_by`. The preparer is not the reviewer.

**The envelope** carries status, effective dates, author, approver and content hash, and rejects:
- an author approving their own version;
- a draft that already has an approver;
- an approved version without one;
- a hash that doesn't match the spec.

**JSON Schema snapshots** of every model live in `close_contracts/schemas/` and are checked for drift by `tools/gen_schemas.py --check`.

What we did not choose:
- A text grammar with a parser. D10 already chose JSON, which needs no parser and maps one-to-one to the IR.
- The `google-re2` package. It would be a new native dependency, and matching RE2's semantics would not guarantee matching Java's anyway.
- Python's private `re._parser` module for validation. It is not a public API.

## Consequences

- Stage 3 compiles the IR to Spark Columns and is held to the interpreter by a differential test over the same vectors and regex corpus.
- A new function or regex construct means changing the catalog, the interpreter, the Spark semantics test and a schema snapshot together, and that shows up as drift at the gate.
- Large integers (above 2^53) cannot appear as literals, only as column values. No realistic amount comes near it: 2^53 cents is about $90 trillion.
- The configuration models are the contract the control-plane API (Stage 4 and 5) will store and serve. Changing them after the Stage 2a gate needs a new version.
