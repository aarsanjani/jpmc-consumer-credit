"""
Measuring Memory Bank rather than describing it.

Four questions, four modules:

    retrieval_eval  When the desk asks the bank a question, how much of what
                    comes back is relevant (precision) and how much of what was
                    relevant comes back (recall)?

    write_policy    When the desk hands the bank a conversation, what does the
                    bank choose to keep, what does it throw away, and what is
                    the mechanism -- topics, scopes, or neither?

    reconcile       When a retrieved memory states a fact, does the warehouse
                    agree? High precision about false things is not a result.

    metrics         The arithmetic, in one place, with no I/O, so the numbers in
                    the report and the numbers in the UI cannot drift apart.

Everything in here reads and writes the live service. Nothing is simulated,
nothing is cached between runs, and every experiment that creates data cleans
up after itself.
"""

from backend.memory_eval import metrics, reconcile, retrieval_eval, write_policy  # noqa: F401
