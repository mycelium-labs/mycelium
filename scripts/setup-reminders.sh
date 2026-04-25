#!/bin/bash
# One-shot: creates 2 recurring macOS reminders for the Mycelium dev rhythm.
#
# Usage:
#   bash scripts/setup-reminders.sh
#
# Idempotent — re-running will create duplicates. If that happens, delete the
# old ones in Reminders.app.
#
# First run will trigger a TCC permission prompt asking the Terminal/Cursor to
# control Reminders.app. Allow it.

set -euo pipefail

LIST_NAME="Mycelium"

osascript <<APPLESCRIPT
tell application "Reminders"
    -- Create the Mycelium list if it doesn't exist
    if not (exists list "$LIST_NAME") then
        make new list with properties {name:"$LIST_NAME"}
    end if

    -- Helper: next occurrence of a weekday at a given hour
    -- (computed in the AppleScript date below)

    -- 1. Weekly: /retro on Sunday 7:00 PM
    set retroDate to date "Sunday 7:00 PM"
    -- AppleScript "Sunday 7:00 PM" parses as the next Sunday in the user's locale
    make new reminder at list "$LIST_NAME" with properties {¬
        name:"/retro on the week", ¬
        body:"Mycelium ritual. In Cursor, run: /retro" & linefeed & ¬
             "Then update LOG.md with the takeaway. ~20 min.", ¬
        due date:retroDate, ¬
        remind me date:retroDate}

    -- 2. Weekly: /plan-ceo-review on Monday 9:00 AM
    set ceoDate to date "Monday 9:00 AM"
    make new reminder at list "$LIST_NAME" with properties {¬
        name:"/plan-ceo-review on this week's plan", ¬
        body:"Mycelium ritual. In Cursor, run: /plan-ceo-review" & linefeed & ¬
             "Feed it: this week's planned work. Mode: HOLD SCOPE unless something changed. ~10 min.", ¬
        due date:ceoDate, ¬
        remind me date:ceoDate}

    -- Note: macOS Reminders doesn't support setting RRULE (weekly recurrence)
    -- via AppleScript. After running this script, open Reminders.app, click each
    -- reminder, and set "Repeat → Weekly" in the info panel. ~30 seconds.
end tell
APPLESCRIPT

cat <<'EOF'

✓ Created two reminders in the "Mycelium" list of Reminders.app.

ONE-TIME MANUAL STEP (AppleScript can't set recurrence — Apple's API limit):
  1. Open Reminders.app → Mycelium list
  2. Click "/retro on the week" → ⓘ info → Repeat: Weekly
  3. Click "/plan-ceo-review on this week's plan" → ⓘ info → Repeat: Weekly

After that they fire forever. You can delete this script.
EOF
