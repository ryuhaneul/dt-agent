# Development task runner for ductor
# Requires: just 1.42.0+ (https://github.com/casey/just)

min_version := "1.42.0"
current_version := `just --version | cut -d' ' -f2`
_check_version := if semver_matches(current_version, ">=" + min_version) == "true" { "" } else { error("just >= " + min_version + " required for [parallel]") }

# Auto-fix formatting and lint issues
fix:
    ruff format .
    ruff check --fix .

# Run the checks CI fails on: i18n completeness, docs bundle, script tests, shell syntax, mypy, and tests (lanes run in parallel; tests stay sequential)
[parallel]
check: _i18n _docs _scripts _types _test

# Informational: ruff lint and format (CI reports these without failing)
lint:
    ruff check ductor_bot/
    ruff format --check ductor_bot/

# Run the test suite sequentially (safe default; see `test-parallel` for opt-in)
test *args:
    pytest {{args}}

# Run the test suite in parallel via pytest-xdist (opt-in; not verified parallel-safe across all 2246+ tests)
test-parallel *args:
    pytest -n auto {{args}}

[private]
_i18n:
    python -m ductor_bot.i18n.check --quiet

[private]
_docs:
    python scripts/build_agent_docs.py --check

[private]
_types:
    mypy ductor_bot/

[private]
_scripts:
    python -m unittest discover -s scripts/tests
    git ls-files -z '*.sh' | xargs -0 -n1 bash -n

[private]
_test:
    pytest
