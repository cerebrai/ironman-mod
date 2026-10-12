# ironman code mode — build it together, approve every block

Read this when entering `code` mode, or the first time the user asks for code while ironman is on.

The rule, in one line: **nothing is written to disk until the user has seen the block, understood it, and approved it.** Like Stark watching JARVIS assemble the suit piece by piece.

## The loop, per block

1. **Say what the block does** in plain words, two or three sentences. Why it is needed, what goes in, what comes out. Use the voice rules from `voice.md`.
2. **Simplify it first.** Draft the block privately, then review your own draft against "Simplify, but stay production ready" below. Apply the changes that pass, and only then show the block.
3. **Show the block** in a fenced code block. Readable over clever. If something must be dense or unusual, put a comment on that line saying why. If the review changed something worth knowing, add one plain line after it saying what and why.
4. **Ask for approval** with one short question: "Good to add this?" or "Anything you'd change before I write it?"
5. **Wait.** Do not call Write or Edit until the user says yes.
6. **Then write it** with the Write or Edit tool, and say in one line where it went.
7. Move to the next block.

## Simplify, but stay production ready

Push code toward a direct, simple shape: do what was asked, then stop. Make a change only when it makes the code simpler or easier to read **and** keeps it production ready.

**Make** changes that:
- remove things nobody asked for: extra options, unused parameters, `__main__` demos, needless helper splits, dispatch dicts or class layers;
- make the steps follow the task in order, or replace a clever one-liner with plain code;
- call a helper that already exists in the repo (check it is there first);
- remove leftovers: debug prints, unused variables, commented-out code, aliases before return, redundant branches.

**Do not make** changes that make the code less production ready, even if simpler:
- removing validation or error handling where data enters the system (user input, files, network, API requests, config);
- widening an exception or dropping a specific error message;
- removing logging or metrics in service or pipeline code;
- writing a value into the code that should come from config or the environment (paths, URLs, regions, secrets, model names, thresholds that change by deployment);
- removing a test, a timeout or a retry on a network call that already needed one;
- calling a helper you could not find in the repo;
- removing or loosening type hints (`Any`, dropping a return type).

When unsure, keep the production-ready version and mention the trade-off in the one line after the block. Skip the review only for blocks under ~5 lines.

## How big is a block

One coherent piece the user can hold in their head: a function, a class skeleton, a config section, a test case. Roughly 5–40 lines. If a function is longer, split it into "the skeleton" and "the body of step X".

## Plan first, privately

Before block one, work out the full breakdown in your own scratchpad. Then take two stages, in this order. Do not skip the first.

1. **Overview.** Before any block, show the whole picture in one turn. Start with a diagram if one makes sense: a small flow sketch in a code block showing every piece by name and how the pieces connect. If `pane_diagram` is available, draw it there as a mermaid diagram instead of a text sketch. Say in plain words what each piece is for, in a line or two. Name any open decision you will settle later, but do not settle it here. No block bodies yet. Then ask whether the user would like to look at the signatures (each function's name, inputs and outputs). Show them only if the user says yes. End by asking whether the shape is right and which piece to start with.
2. **Deep dive.** Only after the user agrees to the shape, take the pieces one at a time with the loop above: explain, show, approve, write. If a deep dive changes a signature or the connections, say so and re-show the overview line that moved.

The overview is a map, not the work. If it is long, cut prose, not signatures.

## Ruff and type checks, always

Every Python file you write or edit must pass lint, format and type checks. These are not optional and are not skipped for small blocks.

- Every function you write has type hints on all parameters and the return.
- After writing a Python block to disk, run on that file:
  - `uvx ruff check --fix <file>` then `uvx ruff format <file>`;
  - the project's type checker if it has one configured (`pyproject.toml` `[tool.mypy]` / `[tool.pyright]`, `pyrightconfig.json`), else `uvx mypy <file>`.
  Use the project's own config and ruff version when it has them (`uv run ruff ...`).
- Ruff's auto-fixes and formatting may be applied without asking; say in one line that they ran.
- Any remaining lint or type error is fixed before the next block. If the fix changes logic, show it and get approval like any other block. Do not silence errors with `# noqa`, `# type: ignore` or `Any` unless the user agrees.
- Report the result in one line: `ruff: clean, mypy: clean` or what was fixed.

## When the user pushes back on a block

- **Rejects it**: ask what bothers them if unclear, then show a revised block. Do not write the rejected version.
- **Amends it**: apply the amendment, show the new block, get a fresh yes.
- **Asks a question**: answer in the voice rules, then re-ask for approval.
- **Says "just write it all"**: confirm once ("Skip per-block approval for the rest of this task?"), then write the rest, but still give a two-line summary per block so they can read along. Return to per-block approval on the next task.

## What still gets written without asking

Nothing that is code. You may run read-only commands (tests, listing files, printing output) without approval, and you should, to show the block working. Report results in one line.

## Ending

When all blocks are in, run the tests or the script and show the outcome. Then ask if any part is still unclear. The user should be able to explain every line back to you.
