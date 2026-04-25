#!/bin/bash
# One-shot: creates 2 weekly macOS reminders for the Mycelium dev rhythm.
#
# Usage:
#   bash scripts/setup-reminders.sh
#
# First run will trigger a TCC permission prompt asking the Terminal/Cursor to
# control Reminders.app. Allow it.
#
# AppleScript can't set "Repeat → Weekly" itself (Apple API limitation).
# After this runs, open Reminders.app and click Repeat: Weekly on each. ~30s.

set -euo pipefail

LIST_NAME="Mycelium"

# Build dates in AppleScript directly (locale-independent — avoids the
# "Sunday 7:00 PM" parse error you'll hit on en_IN, en_GB, etc.)

osascript <<'APPLESCRIPT'
on nextWeekday(targetWeekday, targetHour)
    -- Returns the next occurrence of targetWeekday at targetHour:00 local time.
    -- targetWeekday is one of: Monday, Tuesday, ..., Sunday
    set d to (current date)
    set hours of d to targetHour
    set minutes of d to 0
    set seconds of d to 0
    repeat while (weekday of d) is not targetWeekday
        set d to d + (1 * days)
    end repeat
    if d < (current date) then
        set d to d + (7 * days)
    end if
    return d
end nextWeekday

tell application "Reminders"
    if not (exists list "Mycelium") then
        make new list with properties {name:"Mycelium"}
    end if

    set retroDate to my nextWeekday(Sunday, 19)
    make new reminder at list "Mycelium" with properties ¬
        {name:"/retro on the week", ¬
         body:"Mycelium ritual. In Cursor chat, run: /retro" & linefeed & "Then update LOG.md with the takeaway. ~20 min.", ¬
         due date:retroDate, ¬
         remind me date:retroDate}

    set ceoDate to my nextWeekday(Monday, 9)
    make new reminder at list "Mycelium" with properties ¬
        {name:"/plan-ceo-review on this week's plan", ¬
         body:"Mycelium ritual. In Cursor chat, run: /plan-ceo-review" & linefeed & "Feed it: this week's planned work. Mode: HOLD SCOPE unless something changed. ~10 min.", ¬
         due date:ceoDate, ¬
         remind me date:ceoDate}
end tell
APPLESCRIPT

cat <<'EOF'

✓ Created two reminders in the "Mycelium" list of Reminders.app.

ONE-TIME MANUAL STEP (AppleScript can't set recurrence — Apple API limit):
  1. Open Reminders.app → Mycelium list
  2. Click "/retro on the week"            → ⓘ → Repeat: Weekly
  3. Click "/plan-ceo-review on ..."       → ⓘ → Repeat: Weekly

After that they fire forever. You can delete this script.
EOF
