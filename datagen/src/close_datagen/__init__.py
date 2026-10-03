"""Seeded synthetic marketplace data (Stage 2b): the landing tables, every break type, and an answer key.

Every random value is a pure function of the seed and a stable key (a counter-based generator per entity and
day), so the same seed gives the same data on one core or many, locally or on Databricks.
"""

__version__ = "0.1.0"
