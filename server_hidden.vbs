' Wait for the bot and pass its exit code through, so the QRemoteBotStart task's restart-on-failure still works
WScript.Quit CreateObject("WScript.Shell").Run("""C:\Projects\QRemote\server.cmd""", 0, True)
