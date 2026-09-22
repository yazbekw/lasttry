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
      - "state_change": signal no longer holds (or flipped) → close position
      - "heartbeat": periodic status update
    """

    def __init__(self):
        self._load_config()

    def _load_config(self):
        try:
            from config_manager import config
            self.enabled = bool(config.get('EXTERNAL_BOT_ENABLED'))
            self.url = (config.get('EXTERNAL_BOT_URL') or '').strip()
            self.secret = (config.get('EXTERNAL_BOT_SECRET') or '').strip()
            self.notify_state_change = bool(
                config.get('EXTERNAL_BOT_NOTIFY_STATE_CHANGE', True)
            )
            self.state_change_threshold = float(
                config.get('STATE_CHANGE_THRESHOLD', 30.0)
            )
        except Exception as e:
            logger.warning(f"ExternalBot config load failed: {e}")
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
                    f"status={resp.status_code}"
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
                # Risk management
                'stop_loss': float(signal.stop_loss or 0.0),
                'take_profit': float(signal.take_profit or 0.0),
                'risk_reward_ratio': float(signal.risk_reward_ratio or 0.0),
                'suggested_position_usd': float(signal.suggested_position_usd or 0.0),
                'risk_amount_usd': float(signal.risk_amount_usd or 0.0),
                # Context
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
        """
        Notify the external bot that a previous signal no longer holds.
        The external bot should CLOSE the position.
        """
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
