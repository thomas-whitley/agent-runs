from app.model import ModelReply, StubModel, extract_code


def test_extract_code_takes_the_fenced_block():
    text = "Here is the function.\n```python\ndef add(a, b):\n    return a + b\n```\nDone."

    assert extract_code(text) == "def add(a, b):\n    return a + b"


def test_extract_code_falls_back_to_the_whole_reply():
    assert extract_code("def add(a, b):\n    return a + b") == "def add(a, b):\n    return a + b"


def test_the_stub_model_returns_its_scripted_replies_in_order():
    model = StubModel(replies=["first", "second"])

    assert model.complete(system="s", prompt="p").text == "first"
    assert model.complete(system="s", prompt="p").text == "second"


def test_the_stub_model_repeats_its_last_reply_once_the_script_runs_out():
    model = StubModel(replies=["only"])

    model.complete(system="s", prompt="p")

    assert model.complete(system="s", prompt="p").text == "only"


def test_the_stub_model_reports_tokens_so_the_budget_can_be_spent():
    model = StubModel(replies=["a reply"], tokens_per_reply=120)

    reply = model.complete(system="s", prompt="p")

    assert isinstance(reply, ModelReply)
    assert reply.tokens == 120


def test_the_stub_model_records_the_prompts_it_was_given():
    model = StubModel(replies=["a"])

    model.complete(system="sys", prompt="the failure was: assert 6 == 5")

    assert "assert 6 == 5" in model.prompts[-1]
