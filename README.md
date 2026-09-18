# GridWise API skeleton

Python 3.12 + FastAPI. The HTTP API still uses dummy interpretation and optimization.
A standalone LLM interpreter and LP optimizer are available separately; neither is wired into the API.
The response contains one placeholder `no_op` per note and 24 zero-valued
hourly entries. It is schema-correct, **not a valid energy schedule**.

## Run locally

From this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

No API keys or environment variables are needed for this skeleton.
Service port: **8000**. Interactive API documentation: http://localhost:8000/docs.

## Call the endpoints

```sh
curl -i http://localhost:8000/health
curl -i -X POST http://localhost:8000/optimize-energy \
  -H 'Content-Type: application/json' \
  --data-binary @examples/request.json
```

Both return HTTP 200 for valid requests. `/health` returns `{"status":"ok"}`.
The optimization response echoes `scenario_id` and contains placeholder values.
Malformed JSON, missing fields, invalid types, non-finite/negative energy values,
blank notes, duplicate/missing hours, and inconsistent battery bounds return HTTP 400.
Notes must contain 1–3 non-empty strings; hours must include 0–23 exactly once.

## Docker

```sh
docker build -t gridwise:skeleton .
docker run --rm -p 8000:8000 gridwise:skeleton
```

The container listens on `0.0.0.0:8000`. This creates a local image only;
publishing a registry image and deployment are separate steps.

## Extension points

- `app/schemas.py`: request and response models. Extend the placeholder directive model with the five operational directive types when adding interpretation.
- `app/interpreter.py`: replace `interpret_notes` with LLM extraction and guardrails.
- `app/optimizer.py`: replace `optimize_energy` with scheduling and final validation.
- `app/main.py`: HTTP routing and HTTP 400 validation errors.

Dependencies: FastAPI, Pydantic, Uvicorn. Never commit secrets; `.gitignore`
excludes common secret files and the Docker build copies only application code
and requirements.


## Standalone LLM interpreter (not connected to the API)

```python
from app.llm_interpreter import interpret_notes, validate_directives

notes = [
    "Solar output will drop to about 20% from 1 PM to 3 PM.",
    "Keep the battery at least half full between 6 PM and 9 PM.",
    "The cafeteria menu changes tomorrow.",
]
directives = interpret_notes(notes, capacity_kwh=200)
# interpret_notes already runs validation; this is also callable independently:
checked = validate_directives(directives, note_count=len(notes), capacity_kwh=200)
```

Capacity is required to convert relative reserves and reject reserves above capacity.
The validator requires the original note count so missing trailing notes cannot be
mistaken for a complete result. One invalid entry does not invalidate other notes.
Unsorted/duplicate/out-of-range hours are rejected; entries themselves are returned
in note-index order. Duplicate note mappings fall back rather than choosing one.

The unspecified provider defaults to an OpenAI Chat Completions adapter in
`app/llm_provider.py`. Set `OPENAI_API_KEY` and `OPENAI_MODEL` in your shell
before live use (choose a chat-completions model available to your account).
No model ID is hard-coded and no extra dependencies are needed. `.env` files
are not automatically loaded. A different provider can be supplied using
`completion=callable`, accepting `(system_prompt, user_json)` and returning raw text.

The adapter follows the [official Chat Completions contract](https://platform.openai.com/docs/api-reference/chat/create).
It makes one request with a 20-second network timeout; it does not retry automatically.
The prompt requests a JSON array. Invalid JSON, unsupported output, missing model
configuration, and provider failures produce logged, explicitly labeled `no_op`
fallbacks. Logs exclude raw model responses, notes, credentials and exception text.
A fallback is **not evidence the note is irrelevant**, and cannot guarantee a valid
energy schedule. Future API integration should decide whether to retry or fail a
request when fallbacks occur. Invalid caller inputs raise `ValueError`.

### Tests and the three examples

From the project root, with the local environment activated:

```sh
python -m unittest discover -s tests -v
python -m examples.check_interpreter
```

These run offline with mocked model responses, testing parsing, guardrails and
provider request construction; they do not prove live model understanding.
The example prints the expected 20% solar factor, 100 kWh reserve, and cafeteria
`no_op`. Test against your configured live model with:

```sh
python -m examples.check_interpreter --live
```

The live check makes a real provider request and asserts the example semantics;
it fails if the model misinterprets them or a fallback occurs.


## Standalone LP optimizer (not connected to the API)

```python
from app.schedule_optimizer import optimize_schedule
from app.final_validator import final_validator

result = optimize_schedule(hours, battery, directives)
errors = final_validator(result["hourly_plan"], hours, battery, directives)
assert not errors, errors
```

The result has `hourly_plan`, `total_grid_kwh`, `total_cost_bdt`, and `peak_grid_kwh`.
Totals are calculated from the returned hourly rows, using the input tariffs.
`final_validator` returns `[]` for valid schedules or a list of descriptive errors.

The optimizer uses [SciPy linprog with HiGHS](https://docs.scipy.org/doc/scipy-1.17.0/reference/optimize.linprog-highs.html).
All 24 hours are solved jointly using 96 continuous variables: grid imports,
solar used, signed battery change, and end-of-hour battery energy.
Positive battery change means charging; negative means discharging. A single
signed variable prevents simultaneous charging and discharging without binaries.

Energy balance and battery transitions are equalities. End-of-day energy equals
initial energy as a **hard LP equality**, not a penalty or minimum requirement.
Floating-point solvers still have numerical tolerances: feasibility is configured
at 1e-9, and the returned plan is independently replayed with 1e-7 tolerance.
The public validator defaults to the challenge's 0.01 tolerance. The final
reported battery energy is set to the exact initial input value only after
checking the solved terminal value; replay independently verifies the actions.
No two-decimal rounding is applied to individual plan fields.

The validator rebuilds directive effects independently and replays battery state
from initial energy and action magnitudes. It never trusts the previous row's
reported state or the optimizer's derived bounds. Inputs share only shape/range
validation in `app/schedule_inputs.py`.

Overlapping reserves use the largest minimum; grid caps use the smallest cap;
charge/discharge bans accumulate. The documents do not explicitly define overlapping
solar reductions: this implementation uses the most restrictive factor applied
once to original solar (not multiplied repeatedly). This convention does not
affect any provided public sample.

Malformed inputs/directives raise `ValueError`; no invalid directive is silently
ignored. Infeasible scenarios raise `InfeasibleScheduleError`; solver timeout,
failure, or failed final replay raises `OptimizationError`. No dummy schedule is
returned on optimization failure. The solver has a 10-second time limit.
An explicitly supplied `no_op`, including an interpreter fallback, has no effect;
callers must decide how to handle failed interpretation before invoking the solver.

### Public sample checks

The full unmodified organizer sample pack (inputs and expected outputs) is copied
to `tests/fixtures/public_optimizer_cases.json`, credited to BUP CSE Fest 2026.
No scenarios were pasted with the optimizer request, so the default example uses:

- SAMPLE-05: evening grid cap; reference cost 33950 BDT.
- SAMPLE-10: combined evening reserve/grid cap; reference cost 41620 BDT.

These tests supply reference directives directly; they make no LLM calls.
They compare validity and cost, not exact battery actions, because optimal
schedules may differ.

```sh
pip install -r requirements.txt
python -m examples.check_optimizer
python -m examples.check_optimizer --all
python -m examples.check_optimizer --show-plan
python -m unittest discover -s tests -v
```

The API modules `app/main.py`, `app/interpreter.py`, and `app/optimizer.py`
retain their original dummy behavior. The new solver is in `app/schedule_optimizer.py`.
