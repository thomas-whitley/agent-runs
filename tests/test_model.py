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


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeUsage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeResponse:
    def __init__(self, content, prompt_tokens, completion_tokens):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage(prompt_tokens, completion_tokens)


class _FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _FakeChat:
    def __init__(self, response):
        self.completions = _FakeCompletions(response)


class _FakeOpenAIClient:
    """Stands in for the OpenAI SDK client, which is the boundary we do not own."""

    def __init__(self, content="ok", prompt_tokens=10, completion_tokens=5):
        self.chat = _FakeChat(_FakeResponse(content, prompt_tokens, completion_tokens))


def test_the_openai_compatible_model_returns_the_reply_text_and_total_tokens():
    from app.model import OpenAICompatibleModel

    client = _FakeOpenAIClient(content="```python\ndef add(a, b):\n    return a + b\n```")
    model = OpenAICompatibleModel(model="gemini-3.8-flash", client=client)

    reply = model.complete(system="sys", prompt="do the thing")

    assert "def add" in reply.text
    assert reply.tokens == 15


def test_the_openai_compatible_model_sends_the_system_and_user_messages():
    from app.model import OpenAICompatibleModel

    client = _FakeOpenAIClient()
    model = OpenAICompatibleModel(model="gemini-3.8-flash", client=client)

    model.complete(system="you write python", prompt="write add")

    sent = client.chat.completions.calls[-1]
    assert sent["model"] == "gemini-3.8-flash"
    assert sent["messages"] == [
        {"role": "system", "content": "you write python"},
        {"role": "user", "content": "write add"},
    ]


def test_the_openai_compatible_model_copes_with_an_empty_reply():
    from app.model import OpenAICompatibleModel

    client = _FakeOpenAIClient(content=None)
    model = OpenAICompatibleModel(model="gemini-3.8-flash", client=client)

    assert model.complete(system="s", prompt="p").text == ""
