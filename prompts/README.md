# KERN Prompts

KERN runtime system instructions live here as Markdown so product, security, and corporate reviewers can audit them without reading Python code.

- `system.md`: default local assistant system prompt.
- `rag-system.md`: retrieval-grounded answer system prompt.

Python code loads these files at startup and should not contain the full instruction text inline.
