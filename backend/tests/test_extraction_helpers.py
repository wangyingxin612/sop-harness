"""LLM-free tests for the deterministic parts of PERCEIVE (DESIGN.md §7.3):
the regex floors and the whitelist that override/backstop the model."""
from app.llm.extraction import regex_escalation_floor, whitelist_core_scope


class TestEscalationFloor:
    def test_catches_common_phrasings(self):
        for phrase in [
            "can I talk to a human",
            "I want to speak with a representative",
            "connect me to a person please",
            "get me a manager",
            "transfer me to someone",
        ]:
            assert regex_escalation_floor(phrase), phrase

    def test_does_not_fire_on_unrelated_text(self):
        assert not regex_escalation_floor("I want to talk about my claim")
        assert not regex_escalation_floor("what documents do I need")


class TestWhitelistScope:
    def test_case_id_forces_core(self):
        assert whitelist_core_scope("what's the status of CL-2048")

    def test_document_keyword_forces_core(self):
        assert whitelist_core_scope("I need to submit my pathology report")

    def test_unrelated_text_not_whitelisted(self):
        assert not whitelist_core_scope("what is reinforcement learning")
