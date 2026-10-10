"""Channel selection strategy - core to agent behavior."""

import random
import logging
import pwnagotchi.utils
from .statistics import ChannelStatistics


class ChannelStrategy:
    """
    Intelligent channel selection strategy.

    Strategy:
    - Always scan channels with active APs (high probability of captures)
    - Add random unscanned channels each epoch (explore new areas)
    - Track statistics per channel to guide future decisions
    """

    def __init__(self, config, logger=None):
        """
        Initialize channel strategy.

        Args:
            config: pwnagotchi configuration dict
            logger: optional logger instance
        """
        self.logger = logger or logging.getLogger(__name__)
        self.config = config
        self.stats = ChannelStatistics()

        # Configuration options
        main = config.get("main", {})
        self.extra_channels = main.get("extra_channels", 15)
        self.restrict_channels = main.get("restrict_channels", None)
        self.reset_history = main.get("reset_history", True)

        # Channel-selection mode: "random" (default, original behaviour) or
        # "smart" (yield-weighted). Kept as a toggle so it can be A/B'd in the
        # field before committing to it. Default preserves prior behaviour.
        self.mode = str(main.get("channel_strategy", "random")).lower()

        # Smart-mode tunables (only used when mode == "smart"):
        #  - explore_fraction: share of the extra_channels budget spent on
        #    unproven/unscanned channels instead of proven ones (epsilon).
        #  - yield_weights: relative value of each interaction type when
        #    scoring a channel's past productivity.
        self.explore_fraction = float(main.get("channel_explore_fraction", 0.3))
        self.yield_weights = {
            "Handshakes": float(main.get("channel_weight_handshakes", 5.0)),
            "Associations": float(main.get("channel_weight_assocs", 1.0)),
            "Deauths": float(main.get("channel_weight_deauths", 0.5)),
        }

    def select_channels(self, agent, access_points):
        """
        Select next set of channels to scan based on current APs and unscanned channels.

        Dispatches on self.mode:
          - "random": original behaviour - active channels + N random unscanned.
          - "smart":  yield-weighted - active channels + extra channels chosen
                      by observed productivity, with an exploration fraction
                      still reserved for unproven/unscanned channels.

        Returns list of channels to scan in next epoch.
        """
        try:
            # Update active channels from current scan
            self.stats.update_active_channels(access_points)

            if len(self.stats.unscanned_channels) == 0:
                self._repopulate_unscanned_channels(agent)

            if self.mode == "smart":
                next_channels = self._select_smart(agent)
            else:
                next_channels = self._select_random(agent)

            # Update agent config
            if hasattr(agent, "_config"):
                agent._config["personality"]["channels"] = next_channels

            self.logger.info(
                f"[{self.mode}] Active: {self.stats.active_channels}, "
                f"Next: {next_channels}, "
                f"Unscanned: {len(self.stats.unscanned_channels)}"
            )

            return next_channels

        except Exception as e:
            self.logger.error(f"Error selecting channels: {e}")
            return self.stats.active_channels

    def _select_random(self, agent):
        """Original strategy: active channels + N random unscanned channels."""
        next_channels = self.stats.active_channels.copy()

        for _ in range(self.extra_channels):
            if len(self.stats.unscanned_channels):
                ch = random.choice(list(self.stats.unscanned_channels))
                self.stats.unscanned_channels.remove(ch)
                next_channels.append(ch)

        return next_channels

    def _channel_yield(self, channel):
        """Weighted productivity score for a channel from accumulated stats.

        Reads the per-channel interaction tallies the strategy already keeps in
        ChannelStatistics.chistos (Handshakes / Associations / Deauths) and
        combines them with configurable weights. A channel that has produced
        handshakes scores far higher than one that only ever saw beacons.
        """
        score = 0.0
        for stat_name, weight in self.yield_weights.items():
            per_channel = self.stats.chistos.get(stat_name, {})
            score += weight * per_channel.get(channel, 0)
        return score

    def _select_smart(self, agent):
        """Yield-weighted strategy.

        Budget = extra_channels. A fraction (explore_fraction) is spent on
        exploration (random unscanned channels, as before, so new ground is
        always covered); the remainder goes to the highest-yielding channels
        seen so far that aren't already active. Active channels are always
        scanned - an AP is in range right now.

        Falls back cleanly to pure exploration until any yield history exists,
        so a fresh unit behaves exactly like random mode on its first epochs.
        """
        next_channels = self.stats.active_channels.copy()
        active_set = set(next_channels)

        budget = self.extra_channels
        n_explore = max(1, int(round(budget * self.explore_fraction))) if budget else 0
        n_exploit = budget - n_explore

        # --- exploitation: rank known channels by observed yield ---
        if n_exploit > 0:
            scored = []
            for stat_name in self.yield_weights:
                for ch in self.stats.chistos.get(stat_name, {}):
                    if ch is None or ch < 0 or ch in active_set:
                        continue
                    scored.append(ch)
            # dedupe, score, and take the best n_exploit
            ranked = sorted(
                set(scored), key=self._channel_yield, reverse=True
            )
            for ch in ranked:
                if n_exploit <= 0:
                    break
                if self._channel_yield(ch) <= 0:
                    break  # nothing proven left; let exploration take the rest
                if ch not in next_channels:
                    next_channels.append(ch)
                    if ch in self.stats.unscanned_channels:
                        self.stats.unscanned_channels.remove(ch)
                    n_exploit -= 1

        # roll any unused exploit budget into exploration
        n_explore += max(0, n_exploit)

        # --- exploration: random unscanned channels (original behaviour) ---
        for _ in range(n_explore):
            if len(self.stats.unscanned_channels):
                ch = random.choice(list(self.stats.unscanned_channels))
                self.stats.unscanned_channels.remove(ch)
                if ch not in next_channels:
                    next_channels.append(ch)

        return next_channels

    def _repopulate_unscanned_channels(self, agent):
        """Repopulate unscanned channel list from config or agent."""
        try:
            # Try restrict_channels first
            if self.restrict_channels:
                self.logger.info("Repopulating from restrict_channels")
                self.stats.unscanned_channels = self.restrict_channels.copy()
            # Try agent's allowed channels
            elif hasattr(agent, "_allowed_channels"):
                self.logger.info(f"Repopulating from allowed: {agent._allowed_channels}")
                self.stats.unscanned_channels = agent._allowed_channels.copy()
            # Try agent's supported channels
            elif hasattr(agent, "_supported_channels"):
                self.logger.info("Repopulating from supported")
                self.stats.unscanned_channels = agent._supported_channels.copy()
            # Fall back to all channels for interface
            else:
                self.logger.info("Repopulating from interface channels")
                iface = self.config.get("main", {}).get("iface", "wlan0")
                self.stats.unscanned_channels = pwnagotchi.utils.iface_channels(iface)

        except Exception as e:
            self.logger.warning(f"Error repopulating unscanned channels: {e}")

    def on_wifi_update(self, agent, access_points):
        """Called when agent updates its AP list."""
        self.stats.update_active_channels(access_points)

    def on_association(self, agent, access_point):
        """Called when sending association frame."""
        self.stats.record_interaction("Associations", access_point.get("channel", -1))
        self.stats.mark_ap_seen(access_point, "assoc")

    def on_deauthentication(self, agent, access_point, client_station):
        """Called when sending deauth."""
        self.stats.record_interaction("Deauths", access_point.get("channel", -1))
        self.stats.mark_ap_seen(access_point, "deauth")

    def on_handshake(self, agent, filename, access_point, client_station):
        """Called when handshake is captured."""
        self.stats.record_interaction("Handshakes", access_point.get("channel", -1))
        self.stats.mark_ap_seen(access_point, "handshake")

    def on_bcap_wifi_ap_new(self, agent, event):
        """Called when bettercap detects new AP."""
        try:
            ap = event.get("data", {})
            self.stats.mark_ap_seen(ap)
        except Exception as e:
            self.logger.debug(f"Error on_bcap_wifi_ap_new: {e}")

    def on_bcap_wifi_ap_lost(self, agent, event):
        """Called when bettercap loses AP."""
        try:
            ap = event.get("data", {})
            self.stats.record_ap_lost(ap)
        except Exception as e:
            self.logger.debug(f"Error on_bcap_wifi_ap_lost: {e}")

    def get_stats(self):
        """Get current strategy statistics."""
        return self.stats.get_stats()
