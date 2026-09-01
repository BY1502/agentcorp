# Contributing

## Commit convention

Use English Conventional Commits:

```text
<type>(<scope>): <imperative subject>
```

Allowed types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `style`, `build`, and `ci`. Use lowercase types, concise imperative subjects, no trailing period, and one logical change per commit.

Examples: `feat(skills): implement filesystem skill loader`, `test(core): add phase 2 architecture tests`.

## Branch convention

Use `main` for stable integrated work. Use `feat/<name>`, `fix/<name>`, `refactor/<name>`, and `docs/<name>` for focused changes. Do not introduce GitFlow complexity.
