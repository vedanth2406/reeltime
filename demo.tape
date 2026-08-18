# The README's terminal GIF. Requires vhs: https://github.com/charmbracelet/vhs
#
#   brew install vhs   # or: go install github.com/charmbracelet/vhs@latest
#   vhs demo.tape      # writes demo.gif, next to this file
#
# Run it from the repo root; it records into a temp directory so it never
# touches a .tape you care about.
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
# Remember where the repo is, then work somewhere disposable.
Type "REPO=$PWD; cd $(mktemp -d) && clear"  Enter
Sleep 500ms
Show

# 1. The agent runs and gets the second answer wrong.
Type "tape run python $REPO/examples/truncation_bug.py"  Enter
Sleep 5s

# 2. Replay is instant, offline, and free.
Type "tape ls"  Enter
Sleep 2s
Type "tape replay $(ls .tape/runs | head -1 | cut -c1-8)"  Enter
Sleep 4s

# 3. The bug was never in the code. It was in the context.
Type "tape show $(ls .tape/runs | head -1 | cut -c1-8) 1 --context --diff 0"  Enter
Sleep 8s

# invoice.pdf -- the file the question was about -- was truncated away.
Sleep 3s
