import logging
import json
import time

import requests

import pwnagotchi.plugins as plugins


class PeerDetector(plugins.Plugin):
    __author__ = "fakinthebacon"
    __version__ = "1.0.0"
    __license__ = "GPL3"
    __description__ = (
        "Alerts you when another pwnagotchi (pwngrid peer) is detected nearby. "
        "Works in stealth mode too: pwngrid-peer keeps listening even when our "
        "own advertising is disabled, so you stay quiet but still get warned "
        "about other units in range."
    )

    # Reasonable defaults; everything is overridable via config.
    DEFAULTS = {
        "enabled": False,
        # alert channels
        "log": True,            # always write a WARNING to the pwnagotchi log
        "ui": True,             # show a marker + count on the display
        "webhook": "",          # optional: POST JSON here (Discord/Slack/custom)
        "ntfy_topic": "",       # optional: ntfy.sh topic for phone push
        "ntfy_server": "https://ntfy.sh",
        # behaviour
        "notify_on_lost": False,     # also alert when a peer goes out of range
        "min_rssi": -200,            # ignore peers weaker than this (dBm)
        "cooldown": 30,              # per-peer re-alert cooldown (seconds)
        "request_timeout": 4,        # seconds for webhook/ntfy POSTs
        "ui_position": (0, 95),      # (x, y) of the "near" counter on the display
    }

    def __init__(self):
        self.ready = False
        self._last_alert = {}   # identity -> last alert epoch (seconds)
        self._seen = set()      # identities currently in range

    # ---- config helper -----------------------------------------------------
    def _opt(self, key):
        return self.options.get(key, self.DEFAULTS.get(key))

    # ---- lifecycle ---------------------------------------------------------
    def on_loaded(self):
        logging.info("[peer_detector] plugin loaded")

    def on_ready(self, agent):
        self.ready = True
        logging.info("[peer_detector] ready - watching the mesh for other units")

    def on_unload(self, ui):
        try:
            ui.remove_element("peers_near")
        except Exception:
            pass

    # ---- UI ----------------------------------------------------------------
    def on_ui_setup(self, ui):
        if not self._opt("ui"):
            return
        try:
            from pwnagotchi.ui.components import LabeledValue
            from pwnagotchi.ui.view import BLACK
            import pwnagotchi.ui.fonts as fonts
            pos = self._opt("ui_position")
            if not isinstance(pos, (list, tuple)):
                pos = (0, 95)
            ui.add_element(
                "peers_near",
                LabeledValue(
                    color=BLACK,
                    label="near",
                    value="0",
                    position=tuple(pos),
                    label_font=fonts.Bold,
                    text_font=fonts.Medium,
                ),
            )
        except Exception as e:
            logging.debug("[peer_detector] ui_setup skipped: %s" % e)

    def on_ui_update(self, ui):
        if not self._opt("ui"):
            return
        try:
            ui.set("peers_near", str(len(self._seen)))
        except Exception:
            pass

    # ---- core events -------------------------------------------------------
    def on_peer_detected(self, agent, peer):
        if not self.ready:
            return
        try:
            if int(getattr(peer, "rssi", 0) or 0) < int(self._opt("min_rssi")):
                return
        except (TypeError, ValueError):
            pass

        ident = peer.identity()
        self._seen.add(ident)

        if not self._cooldown_ok(ident):
            return

        name = peer.full_name()
        rssi = getattr(peer, "rssi", 0)
        channel = getattr(peer, "last_channel", "?")
        msg = "another unit nearby: %s (ch %s, %s dBm)" % (name, channel, rssi)
        self._alert("PEER NEARBY", msg, peer, kind="detected")

    def on_peer_lost(self, agent, peer):
        self._seen.discard(peer.identity())
        if not self.ready or not self._opt("notify_on_lost"):
            return
        self._alert("PEER GONE", "unit left range: %s" % peer.full_name(), peer, kind="lost")

    # ---- alerting ----------------------------------------------------------
    def _cooldown_ok(self, ident):
        now = time.time()
        last = self._last_alert.get(ident, 0)
        if now - last < int(self._opt("cooldown")):
            return False
        self._last_alert[ident] = now
        return True

    def _alert(self, title, message, peer, kind):
        if self._opt("log"):
            logging.warning("[peer_detector] %s - %s" % (title, message))

        payload = {
            "event": kind,
            "title": title,
            "message": message,
            "peer": {
                "name": peer.name(),
                "identity": peer.identity(),
                "rssi": getattr(peer, "rssi", 0),
                "channel": getattr(peer, "last_channel", None),
                "encounters": getattr(peer, "encounters", 0),
            },
            "ts": int(time.time()),
        }

        webhook = self._opt("webhook")
        if webhook:
            self._post_webhook(webhook, payload)

        topic = self._opt("ntfy_topic")
        if topic:
            self._post_ntfy(topic, title, message)

    def _post_webhook(self, url, payload):
        try:
            requests.post(
                url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=int(self._opt("request_timeout")),
            )
        except Exception as e:
            logging.debug("[peer_detector] webhook POST failed: %s" % e)

    def _post_ntfy(self, topic, title, message):
        try:
            server = str(self._opt("ntfy_server")).rstrip("/")
            requests.post(
                "%s/%s" % (server, topic),
                data=message.encode("utf-8"),
                headers={"Title": title, "Priority": "high", "Tags": "warning"},
                timeout=int(self._opt("request_timeout")),
            )
        except Exception as e:
            logging.debug("[peer_detector] ntfy POST failed: %s" % e)
