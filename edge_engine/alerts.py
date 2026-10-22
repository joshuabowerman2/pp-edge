"""Phone push (ntfy.sh) and Discord alerts, each sent once."""
import json
import urllib.parse
from datetime import datetime, timezone

from .sources import http


def once(conn, key):
    """True the first time a key is seen."""
    if conn.execute("SELECT 1 FROM alerts_sent WHERE key=?", (key,)).fetchone():
        return False
    conn.execute("INSERT INTO alerts_sent VALUES(?,?)", (key, datetime.now(timezone.utc).isoformat()))
    return True


def send(cfg, title, body, url=None):
    sent = []
    if cfg.ntfy_topic:
        headers = {"Title": title.encode("ascii", "ignore").decode(), "Tags": "chart_with_upwards_trend"}
        if url:
            headers["Click"] = url
        try:
            http.post(f"https://ntfy.sh/{urllib.parse.quote(cfg.ntfy_topic)}", body.encode(), headers)
            sent.append("ntfy")
        except Exception as e:
            print(f"ntfy failed: {e}")
    if cfg.discord_webhook:
        try:
            http.post(cfg.discord_webhook, json.dumps({"content": f"**{title}**\n{body}"[:1990]}).encode(),
                      {"Content-Type": "application/json"})
            sent.append("discord")
        except Exception as e:
            print(f"discord failed: {e}")
    return sent
