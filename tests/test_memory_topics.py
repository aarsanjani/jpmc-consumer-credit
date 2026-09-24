import logging
from pathlib import Path

import pytest
import yaml

from backend.adk_agents import (
    AGENT_MEMORY_TOPICS,
    _topic_filter,
    list_all_agents,
)

TOPICS_YAML = (
    Path(__file__).resolve().parent.parent
    / "jpmc_fraud_demo_kit/jpmc_fraud_demo_kit/memory/custom_topics.yaml"
)


class _FakeBank:
    def __init__(self, labels):
        self._labels = labels

    def memory_topics(self):
        return [{"label": label, "kind": "CUSTOM"} for label in self._labels]


def _declared_topic_ids():
    config = yaml.safe_load(TOPICS_YAML.read_text())
    return {topic["id"] for topic in config["custom_topics"]}


def test_topic_filter_joins_known_topics_with_or():
    bank = _FakeBank(["TRAVEL_STATUS", "CARD_LIFECYCLE"])

    memory_filter = _topic_filter(bank, ["TRAVEL_STATUS", "CARD_LIFECYCLE"])

    assert memory_filter == (
        'topics.custom_memory_topic_label: "TRAVEL_STATUS" OR '
        'topics.custom_memory_topic_label: "CARD_LIFECYCLE"'
    )


def test_topic_filter_drops_unknown_topic_and_warns(caplog):
    bank = _FakeBank(["TRAVEL_STATUS"])

    with caplog.at_level(logging.WARNING):
        memory_filter = _topic_filter(bank, ["TRAVEL_STATUS", "STEP_UP_AUTH"])

    assert memory_filter == 'topics.custom_memory_topic_label: "TRAVEL_STATUS"'
    assert "STEP_UP_AUTH" in caplog.text


@pytest.mark.parametrize(
    ("configured", "requested"),
    [
        (["TRAVEL_STATUS"], []),
        (["TRAVEL_STATUS"], ["STEP_UP_AUTH"]),
        ([], ["TRAVEL_STATUS"]),
    ],
)
def test_topic_filter_returns_none_when_nothing_usable(configured, requested):
    bank = _FakeBank(configured)

    memory_filter = _topic_filter(bank, requested)

    assert memory_filter is None


def test_every_agent_topic_is_declared_in_custom_topics_yaml():
    declared = _declared_topic_ids()

    used = {topic for topics in AGENT_MEMORY_TOPICS.values() for topic in topics}

    assert used <= declared, used - declared


def test_agent_cards_only_list_declared_topics():
    declared = _declared_topic_ids()
    agents = {agent.name: agent for agent in list_all_agents()}

    scoped = {name: set(agents[name].memory_topics_scoped) for name in AGENT_MEMORY_TOPICS}

    assert all(topics <= declared for topics in scoped.values()), scoped
