# PreToolUse hook: deny WebSearch/WebFetch calls whose query/URL touches the task-4
# provider blocklist (Twilio only). All other lookups (Blazor, EF, ASP.NET, ...) pass
# through untouched.
#
# TASK-4 VARIANT (2026-08-11). Task 4 has ONE provider, so the blocklist is Twilio-only:
# a task-4 run has no Maxio or PayPal leg, and blocking terms the task never touches would
# only add noise to the transcript audit. It is a SEPARATE file (selected per arm via
# webGuardFile in the arm config) rather than an edit of the task-1/2/3 guards, so the guard
# those recorded runs received stays byte-identical. Within task 4 the same rule as tasks 1-3
# applies: identical for every guarded arm - tool policy must never differ between arms.
# Keyword filtering is best-effort; the transcript audit is the backstop.
#
# 'twilio' covers its documentation and API hosts (twilio.com, www.twilio.com/docs,
# api.twilio.com, lookups.twilio.com, messaging.twilio.com).
#
# 'mintlify' is here for the same reason it is in the task-1 and task-3 guards: the docs-MCP
# arm is stood up on a Mintlify-hosted docs site, and without this term a docs-MCP agent could
# WebFetch the same pages directly, so the MCP server would stop being that arm's sole
# documentation channel. It goes in the SHARED blocklist, not an arm-specific guard, because
# tool policy must be identical for every guarded arm.
#
# 'sendgrid' is deliberately NOT included: it is a Twilio brand, but it is an email product
# with no bearing on the SMS surface this task builds, and blocking it would deny lookups that
# have nothing to do with the material this guard closes.

$raw = [Console]::In.ReadToEnd()
try { $evt = $raw | ConvertFrom-Json } catch { exit 0 }

if ($evt.tool_name -ne 'WebSearch' -and $evt.tool_name -ne 'WebFetch') { exit 0 }

$parts = @()
if ($evt.tool_input.query)  { $parts += [string]$evt.tool_input.query }
if ($evt.tool_input.url)    { $parts += [string]$evt.tool_input.url }
if ($evt.tool_input.prompt) { $parts += [string]$evt.tool_input.prompt }
$text = $parts -join ' '

# Deliberately NOT blocked: the live API endpoints reached via Bash/curl - self-verification
# against the live account is part of the task for every arm; this guard only closes the
# WebFetch/WebSearch documentation channel.
$blocklist = 'twilio|mintlify'

if ($text -match $blocklist) {
    $decision = @{
        hookSpecificOutput = @{
            hookEventName            = 'PreToolUse'
            permissionDecision       = 'deny'
            permissionDecisionReason = 'Twilio-related web lookups are not permitted in this workspace. Use only the knowledge sources provided inside the workspace.'
        }
    }
    Write-Output ($decision | ConvertTo-Json -Depth 5 -Compress)
}
exit 0
