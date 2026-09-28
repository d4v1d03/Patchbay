# Evals

How prompt, context and model changes are measured, and the results so far.

## The suite

Each task in `evals/tasks/*.yaml` has a prompt, optional setup files or a repo
to clone, hidden tests that are copied into the sandbox only after the agent
finishes, and a check command. A run passes if the check exits 0, or for
`expect: ask_user` tasks, if the agent stops to ask.

| Task | Exercises |
| --- | --- |
| `csv_to_json`, `word_freq`, `stack_calc` | build a small program and its tests from nothing |
| `fix_off_by_one` | fix a bug without touching unrelated code |
| `repo_add_greeting` | work inside a cloned repository |
| `ambiguous_should_ask` | an under-specified task where the right move is `ask_user` |
| `bulk_discount_bug` | a bug across a 300-line, six-module package; the obvious fix is wrong |

```bash
uv run python evals/run.py --trials 3 --prompt v2 --tag mine
uv run python evals/report.py evals/results/latest.json
uv run python evals/report.py --compare v1-flash v2-flash
uv run python evals/judge.py --trials 4 --tag judge        # the reviewer vs hidden tests
```

Setup files are committed before the agent starts, so the diff only shows the
agent's changes. The report gives pass@1, pass@k, steps, tokens, cached share,
cost and time per task, with a 95% Wilson interval on every pass rate;
comparisons only total the tasks both runs share. Costs use DeepSeek's
off-peak prices (`evals/prices.py`) so runs compare regardless of time of day.

## Results

All on `deepseek-flash`, 2026-09-24: about 450 agent runs and 84 judged
reviews, for under $1 in total.

### Prompt v0 → v1: stop when done

v1 added a "Stopping" section (call `finish` as soon as the tests pass, don't
re-verify), told the model to run tests as `python3 -m pytest`, and to use the
workspace listing it was given. Six short tasks × 3 trials:

| | v0 | v1 | |
| --- | --- | --- | --- |
| pass | 18/18 | 18/18 | same |
| steps / task | 4.7 | 3.4 | −28% |
| tokens / task | 12,870 | 8,775 | −32% |
| cost / task | $0.0009 | $0.0007 | −25% |

This was the biggest saving of anything measured.

### Prompt layout and provider caching (v2)

DeepSeek bills a repeated prompt prefix at 2% of the normal price. Within a
session the conversation only grows at the end, so 86–88% of prompt tokens on
the long task were already cached without any changes.

Across sessions it depends on the layout. Tool definitions are sent after the
system prompt, so anything session-specific in the system prompt stops them
from being cached. Cached share of a new session's first request:

| Layout | Cached |
| --- | --- |
| v1: repo, network and file tree in the system prompt | 0% |
| session details at the end of the system prompt | 20% |
| v2: static system prompt, session details in the first user message | 88% |

### Context compaction

`agent/context.py` replaces old tool outputs with a short stub once the
conversation passes a budget, compacting to half the budget so the prefix
stays stable between compactions. Long task, 5 trials each:

| | pass | steps | tokens | cached | cost / task | compacted | re-reads / run |
| --- | --- | --- | --- | --- | --- | --- | --- |
| off | 5/5 | 6.4 | 35.6k | 86% | $0.00165 | 0/5 | 0.0 |
| budget 5k | 5/5 | 6.6 | 35.4k | 88% | $0.00157 | 1/5 | 0.2 |
| budget 3k | 5/5 | 8.0 | 38.5k | 77% | $0.00242 | 5/5 | 1.4 |

The 3k budget cost 47% more: the model re-read files whose contents had been
cut, and each compaction broke the cached prefix. The 5k budget rarely fired
and was within noise. With a 1M-token window and cheap cached tokens, contexts
this size don't need compacting, so it's off by default. It's still there for
long sessions, small-context models or providers without cheap caching. A
summarising variant wasn't built: it costs a model call per compaction and
breaks the cache just the same.

### Variance

`ambiguous_should_ask` passed 3/3 in the v1 baseline, but 11/13 over more
runs; 3/3 is consistent with anything from 44% to 100%. v2 got 12/16 on the
same task (51–90%), so the two prompts can't be told apart on it.

So comparisons that matter use 10+ trials on the task in question. Also,
failures are expensive: a run that built something instead of asking cost
10–20× a passing one (up to 25 steps, $0.012 against $0.0006).

### Thinking: off, low, high

`deepseek-flash` reasons before every reply unless told not to, and
`reasoning_effort` sets how much. Reasoning is billed as output tokens and
sent back with later requests. `LLM_THINKING` sets the level. Seven tasks × 5
trials, prompt v2:

| | off | low | high (provider default) |
| --- | --- | --- | --- |
| pass | 35/35 | 35/35 | 35/35 |
| steps / task | 3.7 | 3.7 | 4.0 |
| tokens / task | 12,044 | 11,694 | 13,975 |
| reasoning tokens / task | 0 | 112 | 189 |
| cost / task | $0.00075 | $0.00072 | $0.00090 |

High cost 20% more than off and 24% more than low (p ≤ 0.001, permutation
test within tasks); off and low were the same (p = 0.49). The reasoning itself
is only ~190 tokens per task, but output tokens cost 4× input, and high also
took 9% more steps. Every level passed everything, so this suite can't show a
case where thinking helps, only what it costs when it isn't needed.

### Reviewer before `finish`

`agent/verifier.py` reviews each ending with a separate model call before the
run is accepted (ARCHITECTURE.md §5).

#### Accuracy

`evals/judge.py` runs the agent without the reviewer, has the reviewer judge
the result, then runs the hidden tests. The agent almost always passes, so a
second model call then plants a bug in the agent's change, the agent's last
command is re-run, and the reviewer judges again. With `--untested` the bug
also has to get past the agent's own tests. 6 tasks × 7 trials, 84 verdicts:

| | reviewer | 95% interval |
| --- | --- | --- |
| agent's work, hidden tests pass: approved | 42/42 | 92–100% |
| planted bug, hidden tests fail: rejected | 20/20 | 84–100% |
| of which the agent's own tests still passed | 11/11 | 74–100% |
| planted change the hidden tests miss: rejected | 12/22 | 35–73% |

The hidden tests aren't a ground truth for the last row, so those 22 were
checked by hand. 15 are real bugs the tests don't cover (`take(xs, 1)` returning `[]`,
a division guard that checks the dividend, an `IndexError` on a short CSV
row), and the reviewer rejected 11. The other 7 change behaviour the task
never specified (negative `n`, extra CLI arguments), and it approved 6. The
four misses were CLI exit codes, argument handling and an `open()` without
`newline=""`, which are easier to find by running the program than by reading
it. It wasn't always consistent either: the same "negative n" change was
rejected once and approved twice.

#### In the loop

`--verify 2` against the same setup without it, 7 tasks × 5 trials:

| | no reviewer | reviewer |
| --- | --- | --- |
| pass | 35/35 | 34/35 |
| rejections | – | 0 of 31 reviews |
| cost / task | $0.00090 | $0.00157 (+74%, p < 0.001) |

No wrong rejections, and no change in pass rate: the agent already passes
everything here, so there's nothing to catch. The one failure is
`ambiguous_should_ask` building instead of asking, which happens without the
reviewer too; the reviewer approved it because the request states no
requirements to check against. A review is about 2,000 tokens, mostly
uncached since every diff is new, plus the reviewer's own thinking, which is
a lot next to a run that is 90% cached.

These runs also turned up two bugs, fixed before the numbers above: a
plain-text ending skipped the review, and the diff was taken against `HEAD`,
so anything the agent committed disappeared from the review and from the
session's saved patch.

## Defaults

* `PROMPT_VERSION=v2`: same pass rate as v1 within what can be measured, and
  88% of a new session's first request cached instead of 0%.
* `CONTEXT_BUDGET_TOKENS=0`: compaction cost more than it saved at these
  context sizes.
* `VERIFY_ROUNDS=0`: the reviewer is accurate here but adds 74% to a run with
  nothing to catch. Worth turning on where the agent's tests are weak or a
  wrong "done" is expensive. A cheaper reviewer (thinking off) hasn't been
  measured yet.
* `LLM_THINKING=off` for DeepSeek (in `.env.example`): 20% cheaper at the same
  pass rate. The setting itself defaults to empty, which sends no thinking
  field, because the field names differ between providers.
