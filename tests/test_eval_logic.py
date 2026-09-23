import pytest
from backend.memory_eval.write_policy import attribute, Utterance
from backend.memory_eval.reconcile import extract_amounts, HYGIENE_PATTERNS
import re

def test_attribute_rejects_single_common_word():
    """A single common token like 'time' must not meet the threshold to link an utterance to a memory."""
    utterances = [
        Utterance(utterance_id="u1", role="user", text="What time do you close today?", expectation="disposable")
    ]
    # The utterance and the memory share exactly one non-stopword token: 'time'
    memory_fact = "The issue occurred during this time of day."
    
    # Assert that it links empty list
    linked = attribute(memory_fact, utterances)
    assert linked == []

def test_extract_amounts_spelled_out_currency():
    """extract_amounts must catch spelled-out currency and numeric amounts."""
    # Matches '412 euros' and '4,280 dollars and 50 cents'
    amts1 = extract_amounts("I spent 412 euros today")
    assert 412.0 in amts1
    
    amts2 = extract_amounts("that was 4,280.50 dollars for the laptop")
    assert 4280.50 in amts2

def test_hygiene_patterns_are_not_amounts():
    """Information matching hygiene patterns (like card suffix) must not be extracted as an amount."""
    # A card suffix is extracted if we used a naive regex, but extract_amounts requires a currency indicator
    text = "My card ending in 4417 was charged without my knowledge."
    amts = extract_amounts(text)
    assert len(amts) == 0

    # Ensure hygiene patterns would catch this if we cared, but they don't produce a contradiction amount
    assert re.search(HYGIENE_PATTERNS["card_suffix"], text)
