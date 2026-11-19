# CrewAI order lookup

A credential-free CrewAI integration walkthrough. It shows how Mycelium binds
tool calls to a stable run identity and how a configured completion contract is
enforced when a crew ends.

The tool is a local fake. The LLM is a scripted stub, so nothing here calls a
provider or model API.

## Install

CrewAI is an optional extra, not a core dependency:

```bash
python -m pip install -e './sdk[crewai]'
```

Without the extra the script still exits 0 and prints
`status=skipped_missing_extra`.

## Run it

From `sdk/`:

```bash
python examples/crewai_fake_order_lookup/run.py
```

Expected last line:

```text
crewai integration walkthrough complete: status=verified run_id_bound=True completion_refused=True completion_accepted=True tool_calls=1
```

## What it demonstrates

1. **Stable identity.** `integrations.crewai.run_id_from: request_id` makes the
   crew read `request_id` from `Crew.kickoff(inputs=...)`. Inside the tool,
   `get_active_execution_scope().run_id` is the host-supplied value, so a retry
   of the same real-world request keeps the same transition identity.
2. **Fail closed on a missing identity.** A kickoff whose `inputs` omit the
   configured field raises `CrewAIIntegrationError` before any tool call.
3. **Completion terminal.** With `completion:` configured, a crew that never
   records its required tool evidence is refused at the real CrewAI terminal.
   The refusal surfaces as CrewAI's `HookAborted` with a
   `CompletionRefusedError` cause.
4. **Accepted evidence.** The same crew finishes normally once the tool marks
   its required subtask.

Automatic CrewAI identity is a dispatch convenience. A consequential production
tool still needs its own stable, host-owned `request_id`; see
[Transition identity](../../README.md#transition-identity-and-host-owned-request_id).

## Requirements

- The `crewai` extra.
- No credentials, network access, Redis, or PostgreSQL.

The example is covered by
[`test_example_crewai_fake_order_lookup.py`](../../tests/test_example_crewai_fake_order_lookup.py),
which skips with a reason when the extra is absent.
