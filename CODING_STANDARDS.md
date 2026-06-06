# Coding Standards

## Variable Names

- Avoid single-letter variable names. Prefer descriptive names with at least two or three characters.
- Use `idx` instead of `i` for indexes.
- Use meaningful names for temporary values, such as `row`, `scenario`, `header_key`, or `header_value`.

## Loop Variables

- When looping over an iterable whose variable name is plural, use the singular form as the loop variable.
- Examples:
  - Use `for row in rows`, not `for r in rows`.
  - Use `for scenario in scenarios`, not `for s in scenarios`.
  - Use `for idx in range(...)`, not `for i in range(...)`.
