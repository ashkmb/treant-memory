# Records docs/assets/demo.gif with VHS (https://github.com/charmbracelet/vhs).
#
# Needs a quick start stack with a chat model for distillation (see README, "Models") and a
# fresh `personal` workspace. From the repository root:
#   TREANT_TOKEN=<token for personal> vhs docs/demo/demo.tape

Output docs/assets/demo.gif

Require python3

Set Shell "bash"
Set FontSize 16
Set Width 1280
Set Height 600
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Framerate 24

Hide
Type "export PS1='\[\e[32m\]$\[\e[0m\] '; alias agent='python3 docs/demo/demo.py'; clear"
Enter
Sleep 500ms
Show

Type "# Monday: the agent finishes a session and saves what it learned"
Enter
Sleep 600ms
Type "agent save"
Enter
Sleep 4s
Type "# Treant's worker distills it into a graph in the background"
Enter
Sleep 2s

Hide
Type "agent wait && clear"
Enter
Wait@180s /^\$$/
Show

Type "# Tuesday: a new session in billing-api starts with a brief"
Enter
Sleep 600ms
Type "agent brief billing-api"
Enter
Sleep 6s
Type `agent search "why were invoices sent twice?"`
Enter
Sleep 6s
