"""Orchestrator behaviour that does not need a live LLM."""

import json

from langchain.messages import AIMessage, HumanMessage

from app import orchestrator
from app.adapters.llm import describe_completion

FINDINGS = json.dumps({"findings": [{"order_id": "L102", "finding_type": "MISSING_INVOICE_CANDIDATE"}]})


def ai(content, finish="stop", output=10, reasoning=8):
        return AIMessage(content=content, response_metadata={
                "finish_reason": finish,
                "token_usage": {"completion_tokens": output, "completion_tokens_details": {"reasoning_tokens": reasoning}},
        })


class FakeInvestigator:
        """Returns the given final replies in turn, appended to the conversation it receives."""

        def __init__(self, *replies):
                self.replies = list(replies)
                self.calls = []

        def invoke(self, payload):
                self.calls.append(payload["messages"])
                return {"messages": payload["messages"] + [self.replies.pop(0)]}


def state():
        return {"request": "Investigate", "findings": "", "retrieved": [], "verdict": None, "attempt": 0, "status": ""}


def test_describe_completion_shows_why_the_model_stopped():
        assert describe_completion(ai("", finish="length", output=16384, reasoning=16000)) == \
                "finish=length, output 16384 tokens (reasoning 16000)"
        assert describe_completion(AIMessage(content="x")) == "finish=?"


def test_empty_reply_gets_one_reminder_in_the_same_conversation(monkeypatch):
        fake = FakeInvestigator(ai("", finish="length"), ai(FINDINGS))
        monkeypatch.setattr(orchestrator, "investigator_agent", fake)
        update = orchestrator.investigate(state())
        assert update["findings"] == FINDINGS
        assert len(fake.calls) == 2
        reminder_conversation = fake.calls[1]
        assert reminder_conversation[0].content == "Investigate"            # original request kept
        assert reminder_conversation[1].content == ""                       # the empty reply kept
        assert isinstance(reminder_conversation[-1], HumanMessage)
        assert reminder_conversation[-1].content == orchestrator.FINDINGS_REMINDER


def test_valid_reply_needs_no_reminder(monkeypatch):
        fake = FakeInvestigator(ai(FINDINGS))
        monkeypatch.setattr(orchestrator, "investigator_agent", fake)
        assert orchestrator.investigate(state())["findings"] == FINDINGS
        assert len(fake.calls) == 1


def test_only_one_reminder_then_code_checks_take_over(monkeypatch):
        fake = FakeInvestigator(ai(""), ai("still not json"))
        monkeypatch.setattr(orchestrator, "investigator_agent", fake)
        assert orchestrator.investigate(state())["findings"] == "still not json"
        assert len(fake.calls) == 2


def retry_state():
        from app.agents.critic_agent import CriticIssue, CriticVerdict
        verdict = CriticVerdict(verdict="RETRY", issues=[
                CriticIssue(order_id="L102", check="code:confidence", problem="confidence must be medium or low")])
        return {**state(), "findings": FINDINGS, "verdict": verdict, "attempt": 1}


def test_retry_gives_the_investigator_the_data_already_retrieved(monkeypatch, tmp_path):
        from app.engines import careops_world
        run_world = careops_world.RunWorld("run-test", "Investigate", directory=tmp_path)
        run_world.record_query("SELECT order_id, invoice_id FROM t", [{"order_id": "L102", "invoice_id": None}], 1, False)
        fake = FakeInvestigator(ai(FINDINGS))
        monkeypatch.setattr(orchestrator, "investigator_agent", fake)
        with careops_world.activate(run_world):
                orchestrator.investigate(retry_state())
        message = fake.calls[0][0].content
        assert "Data already retrieved from Zetaris" in message
        assert '"L102"' in message and '"invoice_id": null' in message
        assert "[code:confidence] L102: confidence must be medium or low" in message
        assert "call ask_data_agent only if" in message


def test_retry_without_a_register_asks_to_retrieve_as_needed(monkeypatch):
        fake = FakeInvestigator(ai(FINDINGS))
        monkeypatch.setattr(orchestrator, "investigator_agent", fake)
        orchestrator.investigate(retry_state())
        message = fake.calls[0][0].content
        assert "Data already retrieved" not in message
        assert message.endswith("Retrieve more data if needed.")


def test_a_critic_that_cannot_answer_ends_the_run_inconclusive_instead_of_failing(monkeypatch):
        def timeout(*_args):
                raise TimeoutError("read timed out")

        monkeypatch.setattr(orchestrator, "review", timeout)
        current = {**state(), "findings": FINDINGS, "attempt": 1, "deadline": float("inf")}
        current.update(orchestrator.critique(current))
        assert current["verdict"] is None and current["stop_reason"] == "critic unavailable (TimeoutError)"
        assert orchestrator.route_after_critique(current) == "finalize"
        assert orchestrator.finalize(current) == {"status": "INCONCLUSIVE", "stop_reason": "critic unavailable (TimeoutError)"}


def test_findings_wrapped_in_a_code_block_or_words_are_still_read(monkeypatch):
        fenced = ai(f"Here are the findings:\n```json\n{FINDINGS}\n```\nLet me know if you need more.")
        fake = FakeInvestigator(fenced)
        monkeypatch.setattr(orchestrator, "investigator_agent", fake)
        result = orchestrator.investigate(state())
        assert json.loads(result["findings"]) == json.loads(FINDINGS)
        assert len(fake.calls) == 1  # no reminder needed
