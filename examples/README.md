# Examples

Three runnable agents. Each one is also an integration test
([`tests/test_examples.py`](../tests/test_examples.py)), so they cannot rot
silently.

| File | What it shows |
|---|---|
| [`openai_agent.py`](openai_agent.py) | Plain OpenAI SDK, three turns, the last one streamed. No reeltime import at all. |
| [`anthropic_agent.py`](anthropic_agent.py) | Plain Anthropic SDK with a system prompt and a tool. No reeltime import at all. |
| [`multi_tool_agent.py`](multi_tool_agent.py) | Local tools via `@tape.tool`, one of them destructive — replay does not delete the file twice. |
| [`m3_replay_speed.py`](m3_replay_speed.py) | Benchmark: record vs replay wall clock, and recording overhead. |

## Running them

```bash
export OPENAI_API_KEY=sk-...          # or ANTHROPIC_API_KEY
tape run python examples/openai_agent.py
tape ls
tape replay <run>                     # offline, free, same output
tape show <run> 1 --context           # what the model actually read
tape show <run> 1 --context --diff 0  # what changed between two calls
```

The two SDK examples import nothing from reeltime. That is the point: `tape run`
injects recording at interpreter startup, so an unmodified agent records itself.

To try them without an API key, point them at any OpenAI- or
Anthropic-compatible endpoint:

```bash
OPENAI_BASE_URL=http://localhost:11434/v1 tape run python examples/openai_agent.py
```

`multi_tool_agent.py` is the one worth running twice. Record it, watch it delete
`temp.log` from a scratch directory, then replay it: the agent reaches the same
decision and reports the same deletion, having deleted nothing.
