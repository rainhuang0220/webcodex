//! Shared native/fallback project text-search exclusions and output bounds.

pub const PROJECT_TEXT_SEARCH_EXCLUDE_GLOBS: &[&str] = &[
    "!.git/**",
    "!**/.git/**",
    "!.claude/**",
    "!**/.claude/**",
    "!target/**",
    "!**/target/**",
    "!node_modules/**",
    "!**/node_modules/**",
    "!**/.venv/**",
    "!**/venv/**",
    "!**/__pycache__/**",
    "!**/.pytest_cache/**",
    "!**/.mypy_cache/**",
    "!**/.ruff_cache/**",
    "!**/.tox/**",
    "!**/site-packages/**",
    "!**/.ipynb_checkpoints/**",
    "!**/dist/**",
    "!**/build/**",
    "!**/coverage/**",
    "!**/.next/**",
    "!secrets/**",
    "!**/secrets/**",
    "!tokens/**",
    "!**/tokens/**",
    "!.env",
    "!**/.env",
    "!.env.*",
    "!**/.env.*",
    "!runner.toml",
    "!**/runner.toml",
    "!agent.toml",
    "!**/agent.toml",
    "!webcodex.env",
    "!**/webcodex.env",
    "!*.pem",
    "!**/*.pem",
    "!*.key",
    "!**/*.key",
];

pub const PROJECT_TEXT_SEARCH_OUTPUT_BYTE_BUDGET: usize = 32 * 1024;

pub fn project_text_search_line_budget(
    limit: usize,
    matches: bool,
    before: usize,
    after: usize,
) -> usize {
    let results = limit.saturating_add(1);
    if !matches || before == 0 && after == 0 {
        return results;
    }
    results
        .saturating_mul(before.saturating_add(after).saturating_add(2))
        .saturating_add(1)
}
