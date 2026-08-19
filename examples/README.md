# Examples

Runnable agents. Each one is also an integration test
([`tests/test_examples.py`](../tests/test_examples.py),
[`tests/test_mcp.py`](../tests/test_mcp.py),
[`tests/test_langchain.py`](../tests/test_langchain.py)), so they cannot rot
silently.

| File | What it shows | Needs a key? |
|---|---|---|
| [`openai_agent.py`](openai_agent.py) | Plain OpenAI SDK, three turns, the last one streamed. No reeltime import at all. | yes |
| [`anthropic_agent.py`](anthropic_agent.py) | Plain Anthropic SDK with a system prompt and a tool. No reeltime import at all. | yes |
| [`multi_tool_agent.py`](multi_tool_agent.py) | Local tools via `@tape.tool`, one of them destructive — replay does not delete the file twice. | yes |
| [`mcp_agent.py`](mcp_agent.py) + [`mcp_server.py`](mcp_server.py) | An MCP session over stdio, recorded as `mcp` events. Replay never starts the server. | **no** |
| [`langchain_agent.py`](langchain_agent.py) | A LangChain/LangGraph agent recorded as chain structure — node, path, depth, fan-out. | **no** |
| [`truncation_bug.py`](truncation_bug.py) | The demo: a context bug that only `--context --diff` makes visible. | **no** |
| [`m1_ambient.py`](m1_ambient.py) | The ambient boundaries — `random`, `uuid`, the clock — recorded and replayed. | **no** |
| [`m3_replay_speed.py`](m3_replay_speed.py) | Benchmark: record vs replay wall clock, and recording overhead. | **no** |

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

## The four worth running twice

`multi_tool_agent.py` — record it, watch it delete `temp.log` from a scratch
directory, then replay it: the agent reaches the same decision and reports the
same deletion, having deleted nothing.

`mcp_agent.py` — record it once, then again with `MCP_EXAMPLE_TOOLS=extended`,
and `tape diff` the two. The server offers one more tool the second time, and
the diff says `+ delete_file` on its own line rather than reporting that two
payloads differ somewhere.

`langchain_agent.py` — the same shape, one layer up. Record it once, then again
with `LANGCHAIN_EXAMPLE_TOOLS=extended`, and the diff names the extra trip round
the graph as extra nodes and a changed fan-out:

```bash
tape run python examples/langchain_agent.py
LANGCHAIN_EXAMPLE_TOOLS=extended tape run python examples/langchain_agent.py
tape diff <first> <second>
```

`truncation_bug.py` — the README's demo, and the shortest path to understanding
why `--context --diff` exists.

Both mock-backed examples bind a fixed port (`REELTIME_DEMO_PORT`,
`LANGCHAIN_EXAMPLE_PORT`) because the request URL is part of what replay matches
on — an ephemeral port would differ between record and replay and every event
would report as drifted.
