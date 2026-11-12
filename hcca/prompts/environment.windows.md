# Windows execution

Commands use PowerShell without profiles. Set variables with `$env:NAME = 'value'`.
PowerShell 5.1 lacks `&&`. Capture native `$LASTEXITCODE` immediately and propagate
it after formatting output. Use literal single quotes and `-LiteralPath` for paths,
with absolute paths when the workspace contains brackets.
Here-string delimiters occupy separate lines. Read UTF-8 explicitly and write
source with `[IO.File]::WriteAllText(..., [Text.UTF8Encoding]::new($false))`.
For multi-layer SSH quoting, use PowerShell `-EncodedCommand` with UTF-16LE Base64.
Launch background helpers with `-WindowStyle Hidden`.
Use the repository's interpreter and test runner. Inspect an existing virtual
environment or conda environment when imports fail, then continue independent checks.
