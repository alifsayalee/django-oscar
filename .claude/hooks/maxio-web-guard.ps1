# PreToolUse hook: deny WebSearch/WebFetch calls whose query/URL touches the Maxio
# blocklist. All other lookups (Blazor, EF, ASP.NET, ...) pass through untouched.
# Identical for every arm - tool policy must never differ between arms.
# Keyword filtering is best-effort; the stage-5 transcript audit is the backstop.

$raw = [Console]::In.ReadToEnd()
try { $evt = $raw | ConvertFrom-Json } catch { exit 0 }

# codex-cli names its web tool 'webrun', NOT WebSearch/WebFetch - MEASURED 2026-08-21 by logging
# tool_name from a real codex PreToolUse payload. That single name mismatch is why every codex run
# before this date had a web guard that was WIRED BUT INERT: the gate below returned at the first
# line and the search proceeded. It is also why the harness's own dry-run text claimed the guard was
# "NOT enforceable" on codex - the conclusion was drawn from the tool never matching, not from the
# hook failing to fire. The hook fires; deny works. Verified end to end: denying webrun took a
# search-demanding prompt from 1 completed search to 0, and the model reported "web search is
# blocked in this environment".
$isClaudeWebTool = ($evt.tool_name -eq 'WebSearch' -or $evt.tool_name -eq 'WebFetch')
$isCodexWebTool  = ($evt.tool_name -eq 'webrun')
if (-not $isClaudeWebTool -and -not $isCodexWebTool) { exit 0 }

$parts = @()
if ($isClaudeWebTool) {
    # UNCHANGED from the original three fields, deliberately: a Claude run's condition must stay
    # byte-identical across this edit so codex and Claude cells remain poolable on tool policy.
    if ($evt.tool_input.query)  { $parts += [string]$evt.tool_input.query }
    if ($evt.tool_input.url)    { $parts += [string]$evt.tool_input.url }
    if ($evt.tool_input.prompt) { $parts += [string]$evt.tool_input.prompt }
} else {
    # webrun's input is NOT a flat query field. Measured shape:
    #   {"search_query":[{"q":"site:docs.maxio.com ..."}],"response_length":"short"}
    # and its action set (read out of codex.exe) is search_query, image_query, OPEN, click, find,
    # screenshot, finance, weather, sports, time - so `open` alone makes it a FETCH tool as well as
    # a search tool. Field-by-field extraction would silently miss every variant but the first, so
    # the whole tool_input is serialised and scanned as text. Any future action codex adds is
    # covered by construction rather than needing another edit here.
    try { $parts += ($evt.tool_input | ConvertTo-Json -Depth 12 -Compress) } catch { $parts += [string]$evt.tool_input }
}
$text = $parts -join ' '

# 'mintlify' (2026-07-29): the maxio-docs-mcp arm's docs site is on a *.mintlify.site host
# whose name matches none of the terms above, so without this an agent could WebFetch the
# same pages directly and the MCP server would stop being the sole Maxio channel. Kept in
# the SHARED blocklist (not an arm-specific guard) so tool policy stays identical across
# arms; it is inert for every other arm.
$blocklist = 'maxio|chargify|advancedbilling|advanced-billing|advanced_billing|AsadAli|ebilling|mintlify'

if ($text -match $blocklist) {
    $decision = @{
        hookSpecificOutput = @{
            hookEventName            = 'PreToolUse'
            permissionDecision       = 'deny'
            permissionDecisionReason = 'Maxio-related web lookups are not permitted in this workspace. Use only the knowledge sources provided inside the workspace.'
        }
    }
    Write-Output ($decision | ConvertTo-Json -Depth 5 -Compress)
}
exit 0
