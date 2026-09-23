# PreToolUse hook: deny WebSearch/WebFetch calls whose query/URL touches the task-3
# provider blocklist (PayPal only). All other lookups (Blazor, EF, ASP.NET, ...) pass
# through untouched.
#
# TASK-3 VARIANT (2026-08-06). Task 3 has ONE provider, so the blocklist is PayPal-only:
# a task-3 run has no Maxio or Twilio leg, and blocking terms the task never touches would
# only add noise to the transcript audit. It is a SEPARATE file (selected per arm via
# webGuardFile in the arm config) rather than an edit of the task-1 or task-2 guard, so the
# guard those recorded runs received stays byte-identical. Within task 3 the same rule as
# tasks 1 and 2 applies: identical for every guarded arm - tool policy must never differ
# between arms. Keyword filtering is best-effort; the transcript audit is the backstop.
#
# 'braintree' is included alongside 'paypal': Braintree is PayPal's own brand and parts of
# the card-vaulting documentation live under braintreepayments.com, so leaving it open would
# be a side door into exactly the material this guard closes for the saved-card flow.
#
# 'mintlify' ADDED 2026-08-11 when the docs-MCP arm was stood up on a Mintlify-hosted PayPal
# docs site. It goes in the SHARED blocklist, not an arm-specific guard, for the same reason
# the task-1 guard carries it: tool policy must be identical for every guarded arm, and
# without it a docs-MCP agent could WebFetch the same pages directly and the MCP server would
# stop being that arm's sole documentation channel. ### THIS CHANGES THE GUARD THE ALREADY-
# RECORDED task3-plugin / task3-openapi RUNS RECEIVED ### - for those arms the term is inert
# in practice (their PayPal lookups were already denied by 'paypal', and no recorded run
# fetched a mintlify host), but a later run of those arms is no longer byte-identical in tool
# policy to the 12 collected on 2026-08-06/07. Disclose it if the two sets are pooled.

$raw = [Console]::In.ReadToEnd()
try { $evt = $raw | ConvertFrom-Json } catch { exit 0 }

if ($evt.tool_name -ne 'WebSearch' -and $evt.tool_name -ne 'WebFetch') { exit 0 }

$parts = @()
if ($evt.tool_input.query)  { $parts += [string]$evt.tool_input.query }
if ($evt.tool_input.url)    { $parts += [string]$evt.tool_input.url }
if ($evt.tool_input.prompt) { $parts += [string]$evt.tool_input.prompt }
$text = $parts -join ' '

# 'paypal' also covers its docs/sandbox hosts (developer.paypal.com,
# api-m.sandbox.paypal.com). Deliberately NOT blocked: the live API endpoints reached via
# Bash/curl - self-verification against the sandbox is part of the task for every arm; this
# guard only closes the WebFetch/WebSearch documentation channel.
$blocklist = 'paypal|braintree|mintlify'

if ($text -match $blocklist) {
    $decision = @{
        hookSpecificOutput = @{
            hookEventName            = 'PreToolUse'
            permissionDecision       = 'deny'
            permissionDecisionReason = 'PayPal-related web lookups are not permitted in this workspace. Use only the knowledge sources provided inside the workspace.'
        }
    }
    Write-Output ($decision | ConvertTo-Json -Depth 5 -Compress)
}
exit 0
