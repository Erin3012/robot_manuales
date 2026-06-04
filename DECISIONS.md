# Decisions

## Why this project uses a lightweight context pack
- The repo is small enough that a short handoff file is cheaper than a full RAG setup.
- A few curated docs are easier to keep current than asking the AI to rescan every file on every chat.

## Documentation convention
- `AI_CONTEXT.md` gives the compact project map.
- `STATUS.md` captures the current working state and what to inspect first.
- `DECISIONS.md` records why the repo is organized this way.

## Maintenance rule
- Update these files when a new entry point, dependency, or major flow is added.
- Keep them short enough that a new chat can read them quickly.

## Automation backend decision
- The project uses Playwright through `browser_compat.py` as the browser automation base.
- Chrome is the default channel; Edge remains available as an explicit fallback when needed.
