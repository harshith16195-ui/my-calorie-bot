#!/usr/bin/env python3
"""Starts Flask + Cloudflare Tunnel, then prints the webhook URL for Twilio."""
import os
import re
import sys
import time
import threading
import subprocess

NGROK_PORT  = 5050
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CLOUDFLARED = os.path.join(SCRIPT_DIR, "cloudflared")

def load_zshrc_env():
    zshrc = os.path.expanduser("~/.zshrc")
    if not os.path.exists(zshrc):
        return
    with open(zshrc) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export ") and "=" in line:
                key, _, val = line[7:].partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key not in os.environ:
                    os.environ[key] = val

def start_flask():
    subprocess.run(
        [sys.executable, "app.py"],
        cwd=SCRIPT_DIR,
        env=os.environ.copy()
    )

def start_tunnel():
    """Launch cloudflared quick-tunnel and capture the public URL."""
    proc = subprocess.Popen(
        [CLOUDFLARED, "tunnel", "--url", f"http://localhost:{NGROK_PORT}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=SCRIPT_DIR,
    )
    url = None
    for line in proc.stdout:
        sys.stdout.write("  [cloudflared] " + line)
        sys.stdout.flush()
        match = re.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", line)
        if match:
            url = match.group(0)
            break
    return proc, url

if __name__ == "__main__":
    load_zshrc_env()

    # Initialise DB
    sys.path.insert(0, SCRIPT_DIR)
    from app import init_db
    init_db()

    # Flask in background thread
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()
    time.sleep(2)

    print("\n🚇 Starting Cloudflare Tunnel...")
    cf_proc, public_url = start_tunnel()

    if not public_url:
        print("❌ Could not get tunnel URL. Check cloudflared output above.")
        sys.exit(1)

    webhook_url = public_url + "/webhook"

    print("\n" + "=" * 60)
    print("✅  My Calorie bot is live!")
    print("=" * 60)
    print(f"\n🌐 Webhook URL:\n   {webhook_url}")
    print("\n📋 Paste this into Twilio Console:")
    print("   console.twilio.com → Messaging → Try it out → Send a WhatsApp message")
    print('   → Sandbox Settings → "When a message comes in"')
    print(f"   Value: {webhook_url}")
    print("   Method: HTTP POST  →  Save")
    print("\n💬 WhatsApp: send 'join <sandbox-word>' to +1 415 523 8886")
    print("   Then type 'hi' to get started!\n")
    print("Press Ctrl+C to stop.\n")

    try:
        flask_thread.join()
    except KeyboardInterrupt:
        cf_proc.terminate()
        print("\nBot stopped.")
