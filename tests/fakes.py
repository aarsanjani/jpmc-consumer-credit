"""In-memory stand-in for backend/memory_bank.py:MemoryBankClient.

Keeps sessions and memories in dicts and matches scopes exactly, which is how
the real service behaves. Shared by the write-path and Action Board tests.
"""

import itertools

PARENT = "projects/p/locations/l/reasoningEngines/e"


def session_name(n: int = 1) -> str:
    return "%s/sessions/s%d" % (PARENT, n)


class FakeMemoryBank:
    def __init__(self, generated_topics=("TRAVEL_STATUS",)):
        self.parent = PARENT
        self.sessions: dict[str, dict] = {}
        self.memories: dict[str, dict] = {}
        self.events: list[tuple[str, str, str]] = []
        self.generate_calls: list[dict] = []
        self.generated_topics = list(generated_topics)
        self._ids = itertools.count(1)

    def add_session(self, name: str, user_id: str) -> None:
        self.sessions[name] = {"name": name, "userId": user_id, "sessionState": {}}

    def create_session(self, user_id, display_name=None, session_state=None):
        name = session_name(100 + len(self.sessions))
        self.add_session(name, user_id)
        self.sessions[name]["sessionState"].update(session_state or {})
        return name

    def _new_memory(self, fact: str, scope: dict, topics: list[str]) -> str:
        name = "%s/memories/m%d" % (PARENT, next(self._ids))
        self.memories[name] = {"name": name, "fact": fact, "scope": dict(scope),
                               "topics": [{"customMemoryTopicLabel": t} for t in topics]}
        return name

    def generate_from_session(self, session_name, scope, disable_consolidation=False, wait=True):
        self.generate_calls.append({"session": session_name, "scope": dict(scope),
                                    "disable_consolidation": disable_consolidation})
        name = self._new_memory("fact from %s" % session_name, scope, self.generated_topics)
        return {"name": "ops/%d" % next(self._ids), "done": True,
                "response": {"generatedMemories": [{"memory": {"name": name}, "action": "CREATED"}]}}

    def create_memory(self, fact, scope):
        return {"done": True, "response": {"name": self._new_memory(fact, scope, [])}}

    def wait_for_operation(self, operation, **_):
        return operation

    def get_memory(self, memory_name):
        return self.memories[memory_name]

    def delete_memory(self, memory_name):
        del self.memories[memory_name]
        return {}

    def retrieve(self, scope, query=None, top_k=5, memory_filter=None):
        rows = [{"memory": dict(m), "distance": float(i)}
                for i, m in enumerate(self.memories.values()) if m["scope"] == scope]
        return rows[:top_k]

    def append_state_delta(self, session_name, author, delta):
        self.sessions[session_name].setdefault("sessionState", {}).update(delta)
        return {}

    def append_event(self, session_name, author, text, **_):
        self.events.append((session_name, author, text))
        return {}

    def get_session(self, session_name):
        return self.sessions[session_name]

    def list_sessions(self):
        return list(self.sessions.values())

    def in_scope(self, scope: dict) -> list[dict]:
        return [m for m in self.memories.values() if m["scope"] == scope]
