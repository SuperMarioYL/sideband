# Sideband demo — replay the bundled trace, then show the artifact.
#
# This tape renders assets/demo.gif from the same commands the committed
# asciinema cast records. The cast is the evidence of record:
#
#   asciinema rec -f asciicast-v2 --window-size 104x40 \
#     -c "bash scripts/demo.sh" assets/demo.cast
#
# Run from the repository root, with the package installed:
#
#   pip install -e .
#   vhs assets/demo.tape

Output assets/demo.gif

Set Shell "bash"
Set FontSize 14
Set FontFamily "JetBrains Mono, Menlo, monospace"
Set Width 1040
Set Height 640
Set Padding 18
Set Margin 0
Set MarginFill "#050712"
Set BorderRadius 8
Set Framerate 30
Set TypingSpeed 45ms
Set Theme {
  "background": "#050712",
  "foreground": "#eef5fd",
  "cursor": "#acccf6",
  "selection": "#171b41",
  "black": "#0c101f",
  "red": "#a46ae7",
  "green": "#8fd6a4",
  "yellow": "#e7d86a",
  "blue": "#666fe8",
  "purple": "#a46ae7",
  "cyan": "#acccf6",
  "white": "#eef5fd",
  "brightBlack": "#959ec4",
  "brightRed": "#c08cf0",
  "brightGreen": "#a6e6b8",
  "brightYellow": "#f0e38a",
  "brightBlue": "#8a93f5",
  "brightPurple": "#c08cf0",
  "brightCyan": "#c7e0ff",
  "brightWhite": "#ffffff"
}

Sleep 600ms
Type "bash scripts/demo.sh"
Enter
Sleep 12s

Type "sed -n '1,24p' channel_budget.json"
Enter
Sleep 9s

Type "python3 -m pytest -q"
Enter
Sleep 9s
