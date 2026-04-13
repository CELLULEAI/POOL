## /cellule command

When the user types `/cellule` (or `/iamine`) followed by a project description, call the **webfetch tool DIRECTLY** with:

- **URL**: `https://cellule.ai/v1/generate-spec`
- **Method**: `POST`
- **Headers**: `{"Content-Type": "application/json"}`
- **Body**: `{"project_name": "<extract from description>", "description": "<full description>", "stack": "<detected stack: python|node|rust|go|...>", "objective": "<one-line objective>"}`

The endpoint returns JSON with two fields: `spec_md` and `opencode_md`.

Then use the **write tool** to:
1. Create or overwrite `SPEC.md` with the value of `spec_md`
2. Create or overwrite `OPENCODE.md` with the value of `opencode_md`

**Important rules:**
- **DO NOT** use bash, shell, or curl — use the **native webfetch tool** of your coding agent
- After writing the files, briefly confirm what was created
- The new `OPENCODE.md` will replace this bootstrap file with project-specific instructions
