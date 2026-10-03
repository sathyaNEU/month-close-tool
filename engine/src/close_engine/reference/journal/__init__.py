"""The reference journaling engine (ADR-0015): landing rows to accounting events to journal lines, in pure
Python. It is the oracle the Spark journaling job will be held to, and it is checked against an independent
journal oracle (``close_engine.reference.journal.oracle``) written from the sample policy's accounting."""
