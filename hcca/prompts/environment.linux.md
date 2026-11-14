# Linux execution

Commands use `/bin/bash` with `pipefail`. Set variables with `export NAME='value'`,
activate with `. path/to/activate`. Pipelines return the rightmost failing
command's status, including checks piped through tail/grep. Quote paths and use
quoted heredoc delimiters for literal scripts.
Use the repository's interpreter and test runner. The runtime Python can differ
from a venv or conda test environment. When pytest is absent, inspect project
instructions before selecting the runner. Continue independent checks after an
environment or network failure and report its effect on acceptance.
