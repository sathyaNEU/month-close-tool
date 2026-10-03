"""The rule DSL (roadmap D10): a typed JSON expression tree, its function catalog, the typecheck to IR,
the Python interpreter and the portable regex subset. No Spark here; Stage 3 compiles the IR to Columns."""

from close_contracts.dsl.ast import Call, Col, DslType, Lit, dump, parse
from close_contracts.dsl.interpret import DslEvalError, evaluate
from close_contracts.dsl.ir import Ir, IrCall, IrCol, IrLit, fingerprint
from close_contracts.dsl.regex import RegexError, SafeRegex
from close_contracts.dsl.typecheck import DslTypeError, Issue, typecheck

__all__ = [
    "Call",
    "Col",
    "DslEvalError",
    "DslType",
    "DslTypeError",
    "Ir",
    "IrCall",
    "IrCol",
    "IrLit",
    "Issue",
    "Lit",
    "RegexError",
    "SafeRegex",
    "dump",
    "evaluate",
    "fingerprint",
    "parse",
    "typecheck",
]
