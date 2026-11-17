from app.application.ask.answer_question import _conversation_state_for_prompt, _summarize_old_messages
from app.infrastructure.persistence.models.ask_conversation import AskMessage


def test_recent_context_keeps_roles_and_bounds_untrusted_history() -> None:
    messages = [AskMessage(role="user", content=f"older-{index}") for index in range(8)]
    messages += [
        AskMessage(role="user", content="Keep the trousers\n\x00" + "x" * 1000),
        AskMessage(role="assistant", content="The shoes stay."),
    ]

    context = _conversation_state_for_prompt(None, [], messages)
    recent = context["recent_messages"]
    assert len(recent) == 6
    assert recent[0] == {"role": "user", "content": "older-4"}
    assert recent[-1] == {"role": "assistant", "content": "The shoes stay."}
    assert len(recent[-2]["content"]) == 600
    assert "\n" not in recent[-2]["content"] and "\x00" not in recent[-2]["content"]
    assert all(set(entry) == {"role", "content"} for entry in recent)


def test_history_never_restores_superseded_wear_feedback_from_an_assistant() -> None:
    messages = [
        AskMessage(role="user", content="Use my saved experience to choose the next outfit"),
        AskMessage(role="assistant", content="Superseded claim: the shoes felt too warm",
                   message_metadata={"wear_feedback_used": True}),
        AskMessage(role="assistant", content="The trousers are still selected"),
    ]
    context = _conversation_state_for_prompt(None, [], messages)
    summary = _summarize_old_messages(messages)

    assert len(context["recent_messages"]) == 2
    assert "Superseded claim" not in str(context)
    assert summary is not None and "Superseded claim" not in summary
    assert "The trousers are still selected" in summary


def test_rolling_summary_keeps_the_latest_complete_entries_in_order() -> None:
    messages = [AskMessage(role="user", content=f"request-{index:02d}-" + "x" * 100) for index in range(40)]
    summary = _summarize_old_messages(messages)

    assert summary is not None and len(summary) <= 600
    assert "request-00" not in summary
    assert "request-39" in summary
    assert summary.index("request-38") < summary.index("request-39")
    assert _summarize_old_messages(messages) == summary
