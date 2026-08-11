# Running the AIDA64 SSE Server on Port 80

Port 80 is a privileged port (< 1024). By default, `root` privileges are required to bind to such a port. Here are two ways to run the script without needing `sudo` every time.

## Option 1: `setcap` (recommended, no sudo needed)

With `setcap` you grant Python3 specifically the permission to bind to privileged ports — no `sudo` needed at startup.

```bash
sudo apt install libcap2-bin   # if not already installed
sudo setcap 'cap_net_bind_service=+ep' $(readlink -f $(which python3))
```

Afterwards you can simply start your script without `sudo`:

```bash
cd ~/homebrew/aida64-knob-linux/ && python3 aida_sse_server.py --port 80
```

### ⚠️ Caution

This grants **any** script running with this Python interpreter the ability to use privileged ports — not just your own script. On a system used only by you, this is usually not a problem, but it isn't entirely clean from a security standpoint.

---

## Option 2: sudoers NOPASSWD rule (targeted for this specific command)

This allows exactly **this one command** to run via `sudo` without a password prompt.

```bash
sudo visudo -f /etc/sudoers.d/aida64knob
```

Add the following line there (adjust the username):

```
yourusername ALL=(ALL) NOPASSWD: /usr/bin/python3 /home/yourusername/homebrew/aida64-knob-linux/aida_sse_server.py --port 80
```

### Important

The path must match **exactly** (including arguments), otherwise the rule won't apply. Verify with `which python3` and the full script path before adding the rule.

---

## Comparison

| Criterion | Option 1: `setcap` | Option 2: `sudoers` |
|---|---|---|
| Requires `sudo` at startup | No | Yes (but without password) |
| Affects only this script | No, affects the entire Python interpreter | Yes, restricted exactly to path + arguments |
| Setup effort | Low | Slightly higher (mind the visudo syntax) |
| Recommendation | Well suited for single-user systems | Useful for more targeted control |
