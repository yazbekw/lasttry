"""
External Bot Integration
- Sends entry signals to an external bot via webhook
- Sends state-change events (close position) when a signal weakens
- HMAC-signed requests for authentication
"""

import os
import hmac
import json
import time
import hashlib
import logging
import requests
from datetime import datetime
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)


class ExternalBotClient:
    """
    Client for sending signals to an external trading bot.

    Events:
      - "entry": actionable signal (BUY/SELL/STRONG)
      - "state_change": signal no longer holds → close position
      - "heartbeat": periodic status update
    """

    def __init__(self):
        # Initialize with safe defaults
        self.enabled = False
        self.url = ''
        self.secret = ''
        self.notify_state_change = False
        self.state_change_threshold = 30.0
        # Try to load config immediately
        self._load_config()

    def _load_config(self):
        """
        Load configuration from config_manager.
        Logs verbosely so we can debug why values come through empty.
        """
        try:
            from config_manager import config

            # Read raw values
            raw_enabled = config.get('EXTERNAL_BOT_ENABLED')
            raw_url = config.get('EXTERNAL_BOT_URL')
            raw_secret = config.get('EXTERNAL_BOT_SECRET')
            raw_notify = config.get('EXTERNAL_BOT_NOTIFY_STATE_CHANGE')
            raw_threshold = config.get('STATE_CHANGE_THRESHOLD')

            logger.info(
                f"ExternalBot._load_config raw values: "
                f"enabled={raw_enabled!r} url={raw_url!r} "
                f"secret_len={len(raw_secret) if raw_secret else 0} "
                f"notify={raw_notify!r} threshold={raw_threshold!r}"
            )

            # Coerce enabled: handle bool, str "true", int 1
            if isinstance(raw_enabled, bool):
                self.enabled = raw_enabled
            elif isinstance(raw_enabled, str):
                self.enabled = raw_enabled.strip().lower() in ('true', '1', 'yes', 'on')
            else:
                self.enabled = bool(raw_enabled)

            self.url = (raw_url or '').strip()
            self.secret = (raw_secret or '').strip()

            if isinstance(raw_notify, bool):
                self.notify_state_change = raw_notify
            else:
                self.notify_state_change = str(raw_notify).lower() in ('true', '1', 'yes', 'on')

            try:
                self.state_change_threshold = float(raw_threshold or 30.0)
            except (ValueError, TypeError):
                self.state_change_threshold = 30.0

            logger.info(
                f"ExternalBot configured: enabled={self.enabled}, "
                f"url_set={bool(self.url)}, secret_set={bool(self.secret)}, "
                f"notify={self.notify_state_change}"
            )
        except Exception as e:
            import traceback
            logger.error(f"ExternalBot config load failed: {e}")
            logger.error(traceback.format_exc())
            # Keep safe defaults
            self.enabled = False
            self.url = ''
            self.secret = ''
            self.notify_state_change = False
            self.state_change_threshold = 30.0

    def reload_config(self):
        """Reload config (call after settings update)."""
        self._load_config()

    # ------------------------------------------------------------------
    def _sign(self, payload_json: str) -> str:
        if not self.secret:
            return ''
        return hmac.new(
            self.secret.encode('utf-8'),
            payload_json.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()

    def _post(self, event_type: str, payload: Dict[str, Any],
              retries: int = 3) -> bool:
        if not self.enabled or not self.url:
            logger.warning(
                f"ExternalBot._post({event_type}) skipped: "
                f"enabled={self.enabled}, url={self.url!r}"
            )
            return False

        payload = dict(payload)
        payload['event'] = event_type
        payload['timestamp'] = datetime.now().isoformat()
        payload['source'] = 'crypto_signal_analyzer_v8'
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'X-Event-Type': event_type,
            'User-Agent': 'CryptoSignalAnalyzer/8.4',
        }
        signature = self._sign(payload_json)
        if signature:
            headers['X-Signature'] = signature

        for attempt in range(1, retries + 1):
            try:
                resp = requests.post(
                    self.url,
                    data=payload_json.encode('utf-8'),
                    headers=headers,
                    timeout=10,
                )
                if 200 <= resp.status_code < 300:
                    logger.info(
                        f"ExternalBot {event_type} sent OK "
                        f"(status={resp.status_code})"
                    )
                    return True
                logger.warning(
                    f"ExternalBot {event_type} attempt {attempt}/{retries} "
                    f"status={resp.status_code} body={resp.text[:200]}"
                )
            except Exception as e:
                logger.warning(
                    f"ExternalBot {event_type} attempt {attempt}/{retries} "
                    f"error: {e}"
                )
            time.sleep(min(2 ** attempt, 8))

        logger.error(f"ExternalBot {event_type} failed after {retries} retries")
        return False

    # ------------------------------------------------------------------
    def send_entry(self, signal, confidence: float = 0.0,
                   signal_id: Optional[int] = None) -> bool:
        """Send an actionable entry signal to the external bot."""
        if not self.enabled:
            return False

        try:
            payload = {
                'symbol': signal.symbol,
                'name': signal.name,
                'signal_type': signal.signal_type.name,
                'direction': 'long' if signal.signal_type.name in
                             ('BUY', 'STRONG_BUY') else 'short',
                'entry_price': float(signal.current_price),
                'score': float(signal.total_score),
                'percentage': float(signal.total_percentage),
                'confidence': float(confidence),
                'signal_id': signal_id,
                'stop_loss': float(signal.stop_loss or 0.0),
                'take_profit': float(signal.take_profit or 0.0),
                'risk_reward_ratio': float(signal.risk_reward_ratio or 0.0),
                'suggested_position_usd': float(signal.suggested_position_usd or 0.0),
                'risk_amount_usd': float(signal.risk_amount_usd or 0.0),
                'btc_bullish': bool(signal.btc_bullish),
                'htf_trend': signal.htf_trend,
                'fear_greed': int(signal.fear_greed_value or 0),
                'atr_value': float(signal.atr_value or 0.0),
                'mtf_details': signal.mtf_details or {},
                'price_change_24h': float(signal.price_change_24h or 0.0),
            }
            return self._post('entry', payload)
        except Exception as e:
            logger.error(f"send_entry payload error: {e}")
            return False

    def send_state_change(self, symbol: str, name: str,
                          previous_type: str, current_type: str,
                          previous_percentage: float,
                          current_percentage: float,
                          current_price: float,
                          reason: str = "signal_weakened") -> bool:
        """Notify external bot that a signal no longer holds."""
        if not self.enabled or not self.notify_state_change:
            return False

        try:
            payload = {
                'symbol': symbol,
                'name': name,
                'previous_signal_type': previous_type,
                'current_signal_type': current_type,
                'previous_percentage': float(previous_percentage),
                'current_percentage': float(current_percentage),
                'current_price': float(current_price),
                'reason': reason,
                'action': 'close_position',
            }
            return self._post('state_change', payload)
        except Exception as e:
            logger.error(f"send_state_change payload error: {e}")
            return False

    def send_heartbeat(self, active_positions: list) -> bool:
        """Periodic heartbeat with all active positions."""
        if not self.enabled:
            return False
        try:
            payload = {
                'active_positions': active_positions,
                'count': len(active_positions),
            }
            return self._post('heartbeat', payload)
        except Exception as e:
            logger.error(f"send_heartbeat error: {e}")
            return False


# Singleton
external_bot = ExternalBotClient()
