# The README's terminal GIF. Requires vhs: https://github.com/charmbracelet/vhs
#
#   brew install vhs   # or: go install github.com/charmbracelet/vhs@latest
#   vhs demo.tape      # writes demo.gif, next to this file
#
# Run it from the repo root; it records into a temp directory so it never
# touches a .tape you care about. If you are working from a clone with a local
# virtualenv, the hidden setup below puts its bin on PATH -- without that, vhs
# spawns a shell that cannot find `tape` and silently records a screen full of
# "command not found".
#
# The demo needs no API key: examples/truncation_bug.py embeds a mock provider,
# so the recording is free and comes out the same for everyone.

Output demo.gif

Set Shell bash
Set FontSize 15
Set Width 1180
Set Height 760
Set Padding 18
Set Theme "Builtin Dark"
Set TypingSpeed 45ms

Hide
# Remember where the repo is; use its virtualenv if there is one, so this works
# from a clone as well as from an installed `pip install reeltime`.
Type "REPO=$PWD; [ -x $REPO/.venv/bin/tape ] && PATH=$REPO/.venv/bin:$PATH"  Enter
Type "cd $(mktemp -d) && cp $REPO/examples/truncation_bug.py . && clear"  Enter
Sleep 500ms
# Fail loudly here rather than recording a blank screen.
Type "command -v tape >/dev/null || echo 'FATAL: tape is not on PATH'"  Enter
Sleep 300ms
Type "clear"  Enter
Show

# 1. The agent runs and gets the second answer wrong.
Type "tape run python truncation_bug.py"  Enter
Sleep 5s

# 2. Replay is instant, offline, and free.
Type "tape replay"  Enter
Sleep 4s

# 3. The bug was never in the code. It was in the context.
Type "tape show last 1 --context --diff 0"  Enter
Sleep 9s

# invoice.pdf -- the file the question was about -- was truncated away.
Sleep 3s
