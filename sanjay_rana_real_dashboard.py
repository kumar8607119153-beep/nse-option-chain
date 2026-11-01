# ============================================================
# SANJAY RANA - DELTA REAL TRADING DASHBOARD
# COMPLETE CONSOLIDATED FILE — QUANTITY-ONLY LOGIC PATCH
# ============================================================

import os
import math
import time
import json
import hmac
import hashlib
import re
import uuid
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from urllib.parse import urlsplit

# ============================================================
# BALANCE-BASED QUANTITY STATE
# The only strategy change: calculate whole-number XRP quantity from live
# available USD on the first valid signal and each confirmed BUY/SELL flip. Delta Exchange India derivative wallets use USD, not USDT.
# Keep the existing trading/API behavior otherwise unchanged.
# ============================================================
QUANTITY_STATE_FILE = os.getenv(
    "SANJAY_QUANTITY_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "quantity_signal_state.json"),
)

def _quantity_from_available_usd(available_balance_usd):
    """Use whole XRP quantities; always choose at least 1 when balance is valid.

    Examples: 10 USD -> 5 XRP, 5 USD -> 2 XRP, below 5 USD -> 1 XRP.
    Exchange margin/available-balance checks still determine whether an order
    can actually be accepted; this sizing rule does not guarantee execution.
    """
    value = float(available_balance_usd)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Available Delta USD balance is invalid")
    if value < 5.0:
        return 1
    return max(1, int(math.floor(value / 2.0)))

def _load_quantity_signal_state():
    try:
        with open(QUANTITY_STATE_FILE, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

def _save_quantity_signal_state(value):
    target = QUANTITY_STATE_FILE
    temp = target + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)

def _available_delta_usd_from_wallet(wallet_response):
    """Return only Delta India's actual USD wallet available for trading.

    Delta Exchange India derivative contracts are quoted/margined/settled in
    USD terms; do not mistake a separate USDT wallet or REF_USD referral asset
    for this USD balance. Deposits and realized profits count only when the
    authenticated wallet API reports them in USD as available_balance.
    Unrealized P&L and total equity are not added separately.
    """
    if not isinstance(wallet_response, dict) or wallet_response.get("success") is not True:
        raise ValueError("Live wallet API did not confirm success")
    rows = wallet_response.get("result")
    if isinstance(rows, dict):
        nested = rows.get("balances")
        rows = nested if isinstance(nested, list) else [rows]
    if not isinstance(rows, list):
        raise ValueError("Wallet API returned no recognized balance list")
    for row in rows:
        if not isinstance(row, dict):
            continue
        asset = str(
            row.get("asset_symbol") or row.get("currency") or
            row.get("asset") or row.get("symbol") or ""
        ).upper().strip()
        if asset != "USD":
            continue
        # Delta API documents available_balance as the amount available for
        # trading. Do not substitute total balance, REF_USD, or other assets.
        for key in ("available_balance", "available_to_trade", "available"):
            if row.get(key) is not None:
                amount = float(row[key])
                if math.isfinite(amount) and amount >= 0:
                    return amount
                raise ValueError("Wallet API returned an invalid available USD balance")
    raise ValueError("Wallet API did not return an actual available-to-trade USD balance for Delta Exchange India")



# ============================================================
# ADDITIVE DISCORD ALERTS — does not alter trading/order logic
# Configure DISCORD_WEBHOOK_URL in Streamlit Secrets or environment.
# ============================================================
def _load_discord_webhook_url():
    value = os.getenv("DISCORD_WEBHOOK_URL", "")
    if value:
        return str(value).strip()
    try:
        return str(st.secrets.get("DISCORD_WEBHOOK_URL", "") or "").strip()
    except Exception:
        return ""


DISCORD_WEBHOOK_URL = _load_discord_webhook_url()


def _discord_alert(alert_id, title, color, fields, footer="Trading Bot • Delta Grid Alert"):
    """Send one Discord embed for a newly detected live event; never replay history."""
    if not DISCORD_WEBHOOK_URL:
        return False
    sent = st.session_state.setdefault("_discord_alert_ids", set())
    if alert_id in sent:
        return False
    payload = {"embeds": [{"title": title, "color": color, "fields": fields,
                           "footer": {"text": footer},
                           "timestamp": datetime.now(timezone.utc).isoformat()}]}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        if response.status_code in (200, 204):
            sent.add(alert_id)
            return True
        print(f"Discord alert failed: HTTP {response.status_code}")
    except Exception as exc:
        print(f"Discord webhook error: {exc}")
    return False


def _discord_field(name, value, inline=True):
    return {"name": str(name), "value": str(value)[:1024] or "-", "inline": inline}


def _discord_register_cycle_event(cycle_key, event_type, event_key):
    """Count each exchange-confirmed target/re-entry once inside its SuperTrend cycle."""
    cycle_key = str(cycle_key or "NO_CYCLE")
    event_type = str(event_type).upper()
    counts_by_cycle = st.session_state.setdefault("_discord_cycle_event_counts", {})
    seen_by_cycle = st.session_state.setdefault("_discord_cycle_counted_event_keys", {})
    counts = counts_by_cycle.setdefault(cycle_key, {"REENTRY": 0, "TARGET": 0, "REENTRY_TARGET": 0})
    seen = seen_by_cycle.setdefault(cycle_key, set())
    unique_key = f"{event_type}|{event_key}"
    if unique_key not in seen:
        seen.add(unique_key)
        counts[event_type] = int(counts.get(event_type, 0)) + 1
    return dict(counts)


def _discord_cycle_count_fields(cycle_key, counts):
    return [
        _discord_field("🔁 SIGNAL CYCLE", str(cycle_key), inline=False),
        _discord_field("📊 RE-ENTRY COUNT THIS SIGNAL", counts.get("REENTRY", 0)),
        _discord_field("🎯 TARGET COUNT THIS SIGNAL", counts.get("TARGET", 0)),
        _discord_field("🔄 RE-ENTRY TARGET COUNT", counts.get("REENTRY_TARGET", 0)),
    ]


def _discord_exact(value, default="-"):
    """Keep exchange-provided decimal precision; never round for display."""
    if value in (None, ""):
        return default
    try:
        from decimal import Decimal
        number_value = Decimal(str(value))
        if not number_value.is_finite():
            return default
        # Decimal(str(...)) preserves the source decimal digits without binary-float rounding.
        return format(number_value, "f")
    except Exception:
        return str(value)[:100]


def _discord_order_text(order, live_price=None):
    """Build a concise order line using only values returned by Delta."""
    oid = order.get("id", order.get("order_id", "-"))
    side = str(order.get("side", "-")).upper()
    order_type = str(order.get("order_type", order.get("type", "-"))).upper()
    state = str(order.get("state", order.get("status", "-"))).upper()
    price = order.get("limit_price", order.get("price", order.get("average_fill_price")))
    qty = order.get("size", order.get("quantity", order.get("order_size", "-")))
    filled = order.get("filled_size", order.get("filled_quantity", order.get("executed_size", 0)))
    remaining = order.get("unfilled_size", order.get("remaining_size"))
    label = order.get("client_order_id", order.get("client_id", order.get("label", "")))
    if remaining in (None, ""):
        try:
            remaining = max(0, float(qty) - float(filled or 0))
        except Exception:
            remaining = "-"
    parts = [f"ID `{oid}` | {side} {order_type} | {state}",
             f"Price `{_discord_exact(price)}` | Qty `{_discord_exact(qty)}`",
             f"Filled `{_discord_exact(filled, '0')}` | Remaining `{_discord_exact(remaining)}`"]
    if live_price is not None:
        parts.append(f"Active Price `{_discord_exact(live_price)}`")
    if label:
        parts.append(f"Tag `{str(label)[:70]}`")
    return "\n".join(parts)


def _discord_send_panel(account_id, panel_name, rows, live_price=None, color=3447003, note=""):
    """Baseline silently, then post only when the real exchange snapshot changes."""
    if not DISCORD_WEBHOOK_URL:
        return False
    rows = list(rows or [])
    lines = [_discord_order_text(row, live_price) for row in rows[:8] if isinstance(row, dict)]
    if not lines:
        lines = ["इस श्रेणी में अभी कोई वास्तविक Exchange रिकॉर्ड नहीं है।"]
    description = "\n\n".join(lines)
    if note:
        description = f"{note}\n\n{description}"
    # Hash order state without the changing live ticker, so a five-second price
    # update does not flood Discord. The current price is included when an order
    # state actually changes and a new panel message is sent.
    stable_lines = [_discord_order_text(row, None) for row in rows[:8] if isinstance(row, dict)]
    stable_description = "\n\n".join(stable_lines) if stable_lines else "इस श्रेणी में अभी कोई वास्तविक Exchange रिकॉर्ड नहीं है।"
    if note:
        stable_description = f"{note}\n\n{stable_description}"
    import hashlib as _hashlib
    snapshot_hash = _hashlib.sha256(stable_description.encode("utf-8")).hexdigest()[:20]
    state_key = f"_discord_panel_state_{str(account_id).replace(' ', '_')}_{panel_name}"
    # First snapshot is only a baseline. Never send old orders/history that
    # already existed before this live session started.
    if state_key not in st.session_state:
        st.session_state[state_key] = snapshot_hash
        return False
    if st.session_state.get(state_key) == snapshot_hash:
        return False
    payload = {"embeds": [{
        "title": f"{account_id} • {panel_name}",
        "description": description[:4000],
        "color": int(color),
        "footer": {"text": "Delta Exchange India • Real API data only • Decimal precision preserved"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }]}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        if response.status_code in (200, 204):
            st.session_state[state_key] = snapshot_hash
            return True
        print(f"Discord {panel_name} panel failed: HTTP {response.status_code}")
    except Exception as exc:
        print(f"Discord {panel_name} panel webhook error: {type(exc).__name__}")
    return False


def _discord_fee_from_fill(fill):
    """Read a fee only when an actual exchange fill/order record supplies it."""
    fee_keys = (
        "commission", "commission_amount", "commission_paid", "commission_fee",
        "fee", "fee_amount", "fill_fee", "trading_fee", "transaction_fee",
        "maker_fee", "taker_fee", "fees",
    )
    for key in fee_keys:
        value = fill.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            try:
                from decimal import Decimal
                Decimal(str(value))
                return key, _discord_exact(value)
            except Exception:
                continue
    # Some API wrappers nest the fee inside a metadata object.
    meta = fill.get("meta_data") if isinstance(fill.get("meta_data"), dict) else {}
    for key in fee_keys:
        value = meta.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            try:
                from decimal import Decimal
                Decimal(str(value))
                return f"meta_data.{key}", _discord_exact(value)
            except Exception:
                continue
    return None, None


def _discord_touch_alert(order, live_price, level_price, touch_time):
    """Send one colourful Discord embed only when a live observed price crosses an active order level."""
    oid = order.get("id", order.get("order_id", "-"))
    side = str(order.get("side", "-")).upper()
    label = str(order.get("client_order_id", order.get("client_id", order.get("label", ""))))
    label_upper = label.upper()
    if "_TGT_" in label_upper or "TARGET" in label_upper or order.get("reduce_only"):
        role, title, color, emoji = "TARGET TOUCH", "🎯 TARGET PRICE TOUCHED", 3066993, "🎯"
    elif "_RE_" in label_upper or "REENTRY" in label_upper:
        role, title, color, emoji = "RE-ENTRY TOUCH", "🔁 RE-ENTRY PRICE TOUCHED", 10181046, "🔁"
    elif "_ENT_" in label_upper or "ENTRY" in label_upper:
        role, title, color, emoji = "ENTRY TOUCH", "📍 ENTRY PRICE TOUCHED", 3447003, "📍"
    elif side == "BUY":
        role, title, color, emoji = "BUY ORDER TOUCH", "🟢 BUY LIMIT PRICE TOUCHED", 5763719, "🟢"
    else:
        role, title, color, emoji = "SELL ORDER TOUCH", "🔴 SELL LIMIT PRICE TOUCHED", 15158332, "🔴"

    alert_id = f"TOUCH_{oid}_{_discord_exact(level_price)}_{touch_time}"
    return _discord_alert(
        alert_id,
        f"{emoji} {title}",
        color,
        [
            _discord_field("Event", role),
            _discord_field("Exchange Order ID", oid),
            _discord_field("Side / Type", f"{side} / {str(order.get('order_type', order.get('type', 'LIMIT'))).upper()}"),
            _discord_field("Order Level", _discord_exact(level_price)),
            _discord_field("Live Market Price at Touch", _discord_exact(live_price)),
            _discord_field("Quantity", _discord_exact(order.get("size", order.get("quantity", order.get("order_size", "-"))))),
            _discord_field("Order Tag", label or "-", inline=False),
            _discord_field("Observed Touch Time", touch_time, inline=False),
            _discord_field("Important", "यह केवल लाइव मार्केट-price touch alert है; इससे order fill की पुष्टि नहीं होती। Fill status Delta Exchange record से अलग verify होगा।", inline=False),
        ],
        "Delta Exchange India • Live observed price touch",
    )


def _discord_check_live_order_touches(open_orders, live_price):
    """Alert only on a price crossing observed during this active dashboard session.

    First observation establishes a baseline and never sends historical/stale alerts.
    A newly seen order is also baseline-only. Reopening the dashboard therefore does
    not replay old touches or old Exchange history.
    """
    try:
        price = float(live_price)
        if not math.isfinite(price) or price <= 0:
            return
    except (TypeError, ValueError):
        return

    active_states = {"open", "pending", "active", "partially_filled", "partiallyfilled", "triggered"}
    current = {}
    for order in (open_orders or []):
        if not isinstance(order, dict):
            continue
        state = str(order.get("state", order.get("status", ""))).lower().replace("-", "_")
        if state and state not in active_states:
            continue
        oid = order.get("id", order.get("order_id"))
        raw_level = order.get("limit_price", order.get("price"))
        if oid in (None, "") or raw_level in (None, ""):
            continue
        try:
            level = float(raw_level)
            if not math.isfinite(level) or level <= 0:
                continue
        except (TypeError, ValueError):
            continue
        current[str(oid)] = (order, level)

    tracker = st.session_state.get("_discord_live_touch_tracker")
    now_text = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    if not isinstance(tracker, dict) or not isinstance(tracker.get("orders"), dict):
        st.session_state["_discord_live_touch_tracker"] = {"last_price": price, "orders": {k: v[1] for k, v in current.items()}}
        return

    previous_price = tracker.get("last_price")
    previous_orders = tracker.get("orders", {})
    try:
        previous_price = float(previous_price)
    except (TypeError, ValueError):
        previous_price = None

    if previous_price is not None:
        for oid, (order, level) in current.items():
            # Only monitor orders already observed on the prior active refresh.
            if oid not in previous_orders:
                continue
            try:
                previous_level = float(previous_orders[oid])
            except (TypeError, ValueError):
                continue
            if abs(previous_level - level) > max(1e-12, abs(level) * 1e-10):
                continue  # changed order price: establish the new level first
            crossed = (previous_price < level <= price) or (previous_price > level >= price)
            if crossed:
                _discord_touch_alert(order, price, level, now_text)

    st.session_state["_discord_live_touch_tracker"] = {"last_price": price, "orders": {k: v[1] for k, v in current.items()}}


def _discord_check_order_lifecycle(open_orders, closed_orders, live_price=None):
    """Post one colourful, separate Discord embed per NEW order/state transition.

    The first exchange snapshot is baseline-only to prevent replaying old history
    when the dashboard opens. Later changes are based on real Delta order records.
    """
    if not DISCORD_WEBHOOK_URL:
        return
    current = {}
    for bucket, rows in (("OPEN", open_orders or []), ("HISTORY", closed_orders or [])):
        for row in rows:
            if not isinstance(row, dict):
                continue
            nested = row.get("order") if isinstance(row.get("order"), dict) else {}
            oid = row.get("id", row.get("order_id", nested.get("id")))
            if oid in (None, ""):
                continue
            oid = str(oid)
            state = str(row.get("state", row.get("status", "open" if bucket == "OPEN" else "unknown"))).upper().replace("-", "_")
            side = str(row.get("side", nested.get("side", "-"))).upper()
            typ = str(row.get("order_type", row.get("type", nested.get("order_type", "LIMIT")))).upper()
            tag = str(row.get("client_order_id", row.get("client_id", row.get("label", ""))) or "")
            price = row.get("limit_price", row.get("price", row.get("average_fill_price")))
            qty = row.get("size", row.get("quantity", row.get("order_size", "-")))
            filled = row.get("filled_size", row.get("filled_quantity", row.get("executed_size", row.get("filled", 0))))
            role_text = tag.upper()
            if any(x in role_text for x in ("_TGT_", "_TARGET_", "_TP_")) or row.get("reduce_only"):
                role = "TARGET / EXIT"
            elif "_RE_" in role_text or "REENTRY" in role_text:
                role = "RE-ENTRY"
            elif "_ENT_" in role_text or "_ENTRY_" in role_text:
                role = "ENTRY"
            else:
                role = "GRID ORDER"
            # Include fill amount in the signature so partial fills are reported too.
            signature = "|".join((bucket, state, side, typ, _discord_exact(price), _discord_exact(qty), _discord_exact(filled, "0"), tag, role))
            current[oid] = {"signature": signature, "bucket": bucket, "state": state, "side": side,
                            "type": typ, "tag": tag, "price": price, "qty": qty, "filled": filled,
                            "role": role, "row": row}

    state_key = "_discord_order_lifecycle_tracker_v2"
    previous = st.session_state.get(state_key)
    if not isinstance(previous, dict):
        # Do not announce old exchange orders/history at first dashboard load.
        st.session_state[state_key] = {k: v["signature"] for k, v in current.items()}
        return

    for oid, item in current.items():
        old_signature = previous.get(oid)
        if old_signature == item["signature"]:
            continue
        # New order on the exchange: it is pending/open, not executed.
        if old_signature is None and item["bucket"] == "OPEN":
            title, color, status = "🟡 PENDING ORDER — EXCHANGE पर लगाया गया", 0xF1C40F, "LIMIT ऑर्डर एक्सचेंज पर मौजूद है; अभी पूर्ण Execute होने की पुष्टि नहीं है।"
        elif item["state"] in {"FILLED", "CLOSED", "COMPLETED"} or (item["bucket"] == "HISTORY" and str(item["filled"]) not in {"0", "0.0", "None", ""}):
            if item["role"] == "TARGET / EXIT":
                title, color, status = "🎯 TARGET ORDER EXECUTED — TARGET HIT", 0x00BFFF, "एक्सचेंज रिकॉर्ड में Target/Exit ऑर्डर Execute/close हुआ है। Entry/Exit और Net P/L का अलग trade-detail संदेश तभी आएगा जब वास्तविक fill/fee डेटा उपलब्ध हो।"
            else:
                title, color, status = "🟢 ORDER EXECUTED — EXCHANGE FILL", 0x2ECC71, "एक्सचेंज रिकॉर्ड में ऑर्डर Fill/Execute हुआ है।"
        elif item["state"] in {"CANCELLED", "CANCELED", "REJECTED", "FAILED", "EXPIRED"}:
            title, color, status = "🔴 ORDER CANCELLED / REJECTED", 0xE74C3C, "एक्सचेंज रिकॉर्ड के अनुसार ऑर्डर Cancel/Reject/Expire हुआ है।"
        elif old_signature is not None and item["state"] in {"OPEN", "PENDING", "ACTIVE", "PARTIALLY_FILLED", "PARTIALLYFILLED"} and str(item["filled"]) not in {"0", "0.0", "None", ""}:
            title, color, status = "🟠 ORDER PARTIALLY FILLED", 0xE67E22, "ऑर्डर का कुछ हिस्सा Execute हुआ है; शेष मात्रा अभी बाकी हो सकती है।"
        elif old_signature is None:
            # New history record with unknown status: don't mislabel as executed.
            title, color, status = "🔵 EXCHANGE ORDER UPDATE", 0x3498DB, "एक्सचेंज से नया ऑर्डर रिकॉर्ड प्राप्त हुआ है; स्थिति नीचे दी गई है।"
        else:
            # Avoid sending routine state noise for unchanged/open records.
            if old_signature.split("|")[1:3] == item["signature"].split("|")[1:3]:
                continue
            title, color, status = "🔵 ORDER STATUS UPDATED", 0x3498DB, "एक्सचेंज पर ऑर्डर की स्थिति बदली है।"

        _discord_alert(
            f"ORDER_LIFECYCLE_{oid}_{hashlib.sha256(item['signature'].encode('utf-8')).hexdigest()[:12]}",
            f"{title} · {item['role']}", color,
            [
                _discord_field("वर्तमान स्थिति", status, inline=False),
                _discord_field("ऑर्डर का प्रकार", item["role"], inline=False),
                _discord_field("Side / Order Type", f"{item['side']} / {item['type']}"),
                _discord_field("Exchange Order ID", oid),
                _discord_field("Order Price", _decimal_7(item["price"])),
                _discord_field("Quantity", _decimal_7(item["qty"])),
                _discord_field("Filled Quantity", _decimal_7(item["filled"], "0.0000000")),
                _discord_field("Live Price at Update", _decimal_7(live_price) if live_price is not None else "उपलब्ध नहीं"),
                _discord_field("Order Tag", item["tag"] or "-", inline=False),
                _discord_field("Exchange Update Time (IST)", datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"), inline=False),
            ],
            "DELTA EXCHANGE INDIA • REAL ORDER STATUS • EACH EVENT IN ITS OWN BLOCK",
        )
    st.session_state[state_key] = {k: v["signature"] for k, v in current.items()}


def _discord_send_order_lifecycle_panels(account_id, open_orders, closed_orders, fills, live_price=None):
    """Separate open-entry, pending, exit, filled, closed, cancelled/rejected, and fee panels."""
    open_orders = [x for x in (open_orders or []) if isinstance(x, dict)]
    closed_orders = [x for x in (closed_orders or []) if isinstance(x, dict)]
    fills = [x for x in (fills or []) if isinstance(x, dict)]

    def state_of(row):
        return str(row.get("state", row.get("status", ""))).lower().replace("-", "_")

    def filled_qty(row):
        for key in ("filled_size", "filled_quantity", "executed_size", "filled"):
            if row.get(key) not in (None, ""):
                try:
                    return float(row[key])
                except Exception:
                    pass
        return 0.0

    def is_exit(row):
        label = " ".join(str(row.get(k, "")) for k in ("client_order_id", "client_id", "label", "reduce_only", "order_type")).lower()
        return bool(row.get("reduce_only")) or any(x in label for x in ("target", "exit", "close", "tp_"))

    active = [o for o in open_orders if state_of(o) in {"open", "pending", "active", "partially_filled", "partiallyfilled"}]
    pending = [o for o in active if filled_qty(o) <= 0 and not is_exit(o)]
    exit_orders = [o for o in active if is_exit(o)]
    open_entry = [o for o in active if not is_exit(o)]
    filled_orders = [o for o in closed_orders if state_of(o) in {"filled", "closed", "completed"} or filled_qty(o) > 0]
    closed_only = [o for o in closed_orders if state_of(o) in {"closed", "filled", "completed"}]
    cancelled_rejected = [o for o in closed_orders if state_of(o) in {"cancelled", "canceled", "rejected", "failed", "expired"}]

    _discord_send_panel(account_id, "OPEN ENTRY / RE-ENTRY ORDERS", open_entry, live_price, 3066993,
                        f"Active entry/re-entry orders: {len(open_entry)}")
    _discord_send_panel(account_id, "PENDING ORDERS", pending, live_price, 16776960,
                        f"Unfilled entry orders waiting on the exchange: {len(pending)}")
    _discord_send_panel(account_id, "EXIT / TARGET ORDERS", exit_orders, live_price, 15158332,
                        f"Active exit/target orders: {len(exit_orders)}")
    _discord_send_panel(account_id, "FILLED ORDERS", filled_orders, live_price, 5763719,
                        f"Exchange history records with execution: {len(filled_orders)}")
    _discord_send_panel(account_id, "CLOSED TRADES / ORDERS", closed_only, live_price, 3447003,
                        f"Exchange closed/filled records: {len(closed_only)}")
    _discord_send_panel(account_id, "CANCELLED / REJECTED ORDERS", cancelled_rejected, live_price, 10038562,
                        f"Cancelled/rejected/expired records: {len(cancelled_rejected)}")

    # One separate fee report, sourced only from actual fill fee fields.
    fee_lines = []
    gross_realized_values = []
    actual_fee_values = []
    fee_currency_seen = set()
    for fill in fills[-8:]:
        fee_key, fee_value = _discord_fee_from_fill(fill)
        oid = fill.get("order_id", (fill.get("order") or {}).get("id", "-"))
        fill_id = fill.get("id", fill.get("fill_id", "-"))
        fee_currency = fill.get("fee_currency", fill.get("commission_asset", fill.get("currency", fill.get("settling_asset_symbol", ""))))
        pnl_value = next((fill.get(k) for k in ("realized_pnl", "realized_profit", "realized_pnl_usd", "realized_profit_loss") if fill.get(k) not in (None, "")), None)
        if pnl_value is not None:
            try:
                from decimal import Decimal as _Decimal
                gross_realized_values.append(_Decimal(str(pnl_value)))
            except Exception:
                pass
        if fee_value is not None:
            try:
                from decimal import Decimal as _Decimal
                actual_fee_values.append(_Decimal(str(fee_value)))
                if fee_currency:
                    fee_currency_seen.add(str(fee_currency))
            except Exception:
                pass
        if fee_value is None and pnl_value is None:
            continue
        fee_lines.append(
            f"Fill ID `{fill_id}` | Order ID `{oid}` | Side `{str(fill.get('side', '-')).upper()}`\n"
            f"Fill Price `{_discord_exact(fill.get('price'))}` | Qty `{_discord_exact(fill.get('size', fill.get('quantity')))}`\n"
            f"Actual {fee_key} `{fee_value}` {fee_currency}" if fee_value is not None else
            f"Fill ID `{fill_id}` | Order ID `{oid}` | Realized P/L `{_discord_exact(pnl_value)}`"
        )
    if gross_realized_values:
        gross_total = sum(gross_realized_values)
        fee_total = sum(actual_fee_values) if actual_fee_values else None
        fee_currency_label = ", ".join(sorted(fee_currency_seen)) or "API fee currency not specified"
        fee_lines.insert(0, f"Gross realized P/L reported by fills: `{format(gross_total, 'f')}`")
        if fee_total is not None:
            fee_lines.insert(1, f"Actual fill fees summed: `{format(fee_total, 'f')}` {fee_currency_label}")
            fee_lines.insert(2, f"Net realized P/L (gross less listed fees): `{format(gross_total - fee_total, 'f')}` {fee_currency_label}")
        else:
            fee_lines.insert(1, "Net P/L not calculated: actual fee fields were not returned for these fills.")
    else:
        fee_lines.insert(0, "Exchange fill rows did not expose a recognized realized-P/L field; net profit is not guessed.")
    _discord_send_panel(account_id, "ACTUAL FILL FEES & NET P/L", [], live_price, 15105570,
                        "\n\n".join(fee_lines) if fee_lines else
                        "Delta fills API ने कोई fill record नहीं लौटाया; अनुमानित फीस या P/L नहीं दिखाया गया।")


def _decimal_7(value, default="UNAVAILABLE"):
    """Format numeric exchange values to seven decimal places (0.0000001 precision)."""
    if value in (None, ""):
        return default
    try:
        from decimal import Decimal, InvalidOperation
        number_value = Decimal(str(value))
        if not number_value.is_finite():
            return default
        return format(number_value, ".7f")
    except Exception:
        return default


def _grid_trade_report_rows(open_orders, closed_orders, fills):
    """Prepare real-exchange open orders and target/entry pairs without inventing P/L."""
    from decimal import Decimal
    open_rows = [r for r in (open_orders or []) if isinstance(r, dict)]
    closed_rows = [r for r in (closed_orders or []) if isinstance(r, dict)]
    fill_rows = [r for r in (fills or []) if isinstance(r, dict)]

    def oid(row):
        nested = row.get("order") if isinstance(row.get("order"), dict) else {}
        value = row.get("id", row.get("order_id", nested.get("id")))
        return str(value) if value not in (None, "") else ""

    def tag(row):
        return str(row.get("client_order_id", row.get("client_id", row.get("label", ""))) or "")

    def role(row):
        t = tag(row).upper()
        if any(x in t for x in ("_TGT_", "_TARGET_", "_TP_")):
            return "TARGET"
        if any(x in t for x in ("_RE_", "REENTRY")):
            return "RE-ENTRY"
        if any(x in t for x in ("_ENT_", "_ENTRY_")):
            return "ENTRY"
        return "ORDER"

    def level_key(row):
        # Normalize role-specific tags so ENTRY_<cycle>_L8, TARGET_<cycle>_L8,
        # and REENTRY_<cycle>_L8 map to the same <cycle>_L8 key.
        raw_tag = tag(row)
        nested = row.get("order") if isinstance(row.get("order"), dict) else {}
        if not raw_tag:
            raw_tag = str(nested.get("client_order_id", nested.get("client_id", nested.get("label", ""))) or "")
        match = re.match(r"^(?:ENTRY|TARGET|REENTRY)_(.+?_L\d+)(?:_|$)", raw_tag, re.IGNORECASE)
        if match:
            return match.group(1).upper()
        # Older generated client IDs may place role code after the level.
        match = re.search(r"_(?:ENTRY|TARGET|REENTRY)?(?:_)?L(\d+)(?:_|$)", raw_tag, re.IGNORECASE)
        return f"L{match.group(1)}" if match else ""

    by_order = {}
    for f in fill_rows:
        key = oid(f)
        if key:
            by_order.setdefault(key, []).append(f)

    def fill_stats(row):
        records = by_order.get(oid(row), [])
        qty_sum = Decimal("0")
        value_sum = Decimal("0")
        fee_sum = Decimal("0")
        fee_found = False
        fee_currencies = set()
        pnl_sum = Decimal("0")
        pnl_found = False
        for f in records:
            try:
                q = abs(Decimal(str(f.get("size", f.get("filled_size", f.get("quantity", 0))) or 0)))
                px = Decimal(str(f.get("price")))
                if q > 0:
                    qty_sum += q
                    value_sum += q * px
            except Exception:
                pass
            fee_key, fee_val = _discord_fee_from_fill(f)
            if fee_val is not None:
                try:
                    # Delta reports negative commission when a maker rebate is earned.
                    # Preserve the sign so net = gross - signed commission is correct.
                    fee_sum += Decimal(str(fee_val))
                    fee_found = True
                    currency = f.get("fee_currency", f.get("commission_asset", f.get("currency", f.get("settling_asset_symbol"))))
                    if currency:
                        fee_currencies.add(str(currency))
                except Exception:
                    pass
            _direct_pnl_found = False
            for k in ("realized_pnl", "realized_profit", "realized_pnl_usd", "realized_profit_loss"):
                if f.get(k) not in (None, ""):
                    try:
                        pnl_sum += Decimal(str(f[k]))
                        pnl_found = True
                        _direct_pnl_found = True
                    except Exception:
                        pass
                    break
            # Delta's documented fills response can put realized P/L in
            # meta_data.new_position.realized_pnl rather than at the fill root.
            # This is a position snapshot, so retain the latest snapshot value
            # only when the fill itself did not provide a direct P/L value.
            if not _direct_pnl_found:
                _meta = f.get("meta_data") if isinstance(f.get("meta_data"), dict) else {}
                _new_pos = _meta.get("new_position") if isinstance(_meta.get("new_position"), dict) else {}
                _nested_pnl = _new_pos.get("realized_pnl")
                if _nested_pnl not in (None, ""):
                    try:
                        pnl_sum = Decimal(str(_nested_pnl))
                        pnl_found = True
                    except Exception:
                        pass
        avg = value_sum / qty_sum if qty_sum > 0 else None
        if avg is None:
            raw = row.get("average_fill_price", row.get("avg_fill_price"))
            try:
                avg = Decimal(str(raw)) if raw not in (None, "") else None
            except Exception:
                avg = None
        # If fills pagination omitted a fill row but the exchange order record
        # explicitly reports executed quantity, use only that executed amount.
        row_filled_qty = None
        for qkey in ("filled_size", "filled_quantity", "executed_size", "filled"):
            qraw = row.get(qkey)
            if qraw not in (None, ""):
                try:
                    qvalue = abs(Decimal(str(qraw)))
                    if qvalue > 0:
                        row_filled_qty = qvalue
                        break
                except Exception:
                    pass
        final_qty = qty_sum if qty_sum > 0 else row_filled_qty
        # Preserve exchange fee currency when it is present in any actual fill.
        return {"qty": final_qty, "avg": avg,
                "fee": fee_sum if fee_found else None, "fee_currencies": fee_currencies,
                "pnl": pnl_sum if pnl_found else None, "records": records}

    open_display = []
    for row in open_rows:
        state = str(row.get("state", row.get("status", "OPEN"))).upper()
        price = row.get("limit_price", row.get("price"))
        qty = row.get("size", row.get("quantity", row.get("order_size")))
        filled = row.get("filled_size", row.get("filled_quantity", row.get("executed_size", 0)))
        remaining = row.get("unfilled_size", row.get("remaining_size"))
        if remaining in (None, ""):
            try:
                remaining = max(0, float(qty) - float(filled or 0))
            except Exception:
                remaining = None
        open_display.append({"id": oid(row) or "-", "side": str(row.get("side", "-")).upper(),
            "type": str(row.get("order_type", row.get("type", "LIMIT"))).upper(), "state": state,
            "price": price, "qty": qty, "filled": filled, "remaining": remaining,
            "tag": tag(row), "role": role(row)})

    # Pair only actual filled/closed TARGET records with the latest prior filled
    # ENTRY/RE-ENTRY at the same grid level. Net P/L is withheld unless exchange
    # reports realized P/L and actual fees in a compatible known currency.
    completed = []
    candidates = [r for r in closed_rows if role(r) in {"ENTRY", "RE-ENTRY"}]
    for exit_row in closed_rows:
        if role(exit_row) != "TARGET":
            continue
        state = str(exit_row.get("state", exit_row.get("status", ""))).lower()
        xstats = fill_stats(exit_row)
        if not (state in {"filled", "closed", "completed"} or xstats["qty"]):
            continue
        key = level_key(exit_row)
        possible = [r for r in candidates if key and level_key(r) == key and
                    (str(r.get("state", r.get("status", ""))).lower() in {"filled", "closed", "completed"} or fill_stats(r)["qty"])]
        if not possible:
            continue
        # The latest candidate by exchange timestamp is selected; no price-based guesses.
        def timestamp(r):
            for k in ("updated_at", "closed_at", "filled_at", "created_at", "created_time", "timestamp"):
                if r.get(k) not in (None, ""):
                    try:
                        v = float(r[k]); return v / 1000 if v > 10_000_000_000 else v
                    except Exception:
                        pass
            return 0
        possible.sort(key=timestamp)
        entry_row = possible[-1]
        estats = fill_stats(entry_row)
        # Prefer exchange-reported realized P/L. Some fills responses attach it
        # to the closed order instead of the individual fill rows.
        gross = xstats["pnl"]
        if gross is None:
            for _pnl_key in ("realized_pnl", "realized_profit", "realized_pnl_usd", "realized_profit_loss"):
                _pnl_raw = exit_row.get(_pnl_key)
                if _pnl_raw not in (None, ""):
                    try:
                        gross = Decimal(str(_pnl_raw))
                        break
                    except Exception:
                        pass
        # XRPUSD in this dashboard is configured as 1 XRP per contract.
        # If Delta omits a realized-P/L field but both actual execution prices
        # and executed quantities are present, calculate gross P/L from those
        # exchange records (never from the planned limit price alone).
        if gross is None and estats["avg"] is not None and xstats["avg"] is not None:
            _entry_qty = estats["qty"]
            _exit_qty = xstats["qty"]
            _matched_qty = min(_entry_qty, _exit_qty) if _entry_qty is not None and _exit_qty is not None else None
            _entry_side = str(entry_row.get("side", "")).upper()
            if _matched_qty is not None and _matched_qty > 0 and _entry_side in {"BUY", "SELL"}:
                _price_delta = xstats["avg"] - estats["avg"]
                gross = _price_delta * _matched_qty if _entry_side == "BUY" else -_price_delta * _matched_qty
        fees_known = estats["fee"] is not None and xstats["fee"] is not None
        fee_currencies = estats["fee_currencies"] | xstats["fee_currencies"]
        fee_currency_compatible = (
            len(fee_currencies) == 1
            and next(iter(fee_currencies)).strip().upper() in {"USD", "USDT", "USDC"}
        )
        net = gross - estats["fee"] - xstats["fee"] if gross is not None and fees_known and fee_currency_compatible else None
        entry_px = estats["avg"]
        exit_px = xstats["avg"]
        completed.append({"entry": entry_row, "exit": exit_row, "entry_stats": estats,
                          "exit_stats": xstats, "entry_price": entry_px, "exit_price": exit_px,
                          "gross": gross, "fees": (estats["fee"] + xstats["fee"]) if fees_known else None,
                          "net": net, "fee_currency": next(iter(fee_currencies), "Exchange fee currency unavailable")})
    completed.sort(key=lambda x: timestamp(x["exit"]), reverse=True)
    return open_display, completed[:30]


def _discord_supertrend_signal(symbol, action, position_status, price, quantity, candle_time,
                               old_signal="UNKNOWN", actual_action="Signal detected; exchange order status is reported separately"):
    """Send one rich, colour-coded SuperTrend flip embed; never claim an unverified fill."""
    action, position_status = str(action).upper(), str(position_status).upper()
    old_signal = str(old_signal or "UNKNOWN").upper()
    colors = {"BUY": 0x2ECC71, "SELL": 0xE74C3C}
    icon = "🟢" if action == "BUY" else "🔴"
    return _discord_alert(
        f"ST_{symbol}_{old_signal}_{action}_{position_status}_{candle_time}",
        f"{icon} SUPERTREND SIGNAL CHANGE — {old_signal} → {action} · {symbol}",
        colors.get(action, 0x3498DB),
        [
            _discord_field("🚦 SIGNAL CHANGE", f"**{old_signal} → {action}**", inline=False),
            _discord_field("⏱️ STRATEGY SETTINGS", "Timeframe: **1H** · ATR: **10** · Multiplier: **2.0** · Source: **HL2** · Confirmed candle close", inline=False),
            _discord_field("🕯️ CONFIRMED CANDLE TIME (IST)", candle_time, inline=False),
            _discord_field("🎬 ACTUAL ACTION", actual_action, inline=False),
            _discord_field("📍 SIGNAL PRICE", _discord_exact(price)),
            _discord_field("📦 SIGNAL QUANTITY", f"{quantity} {symbol}"),
            _discord_field("🧭 BOT POSITION STATUS", position_status),
            _discord_field("🔄 NEW SIGNAL CYCLE", "Re-entry / Target counters reset to 0 for this confirmed signal", inline=False),
            _discord_field("🕒 ALERT TIME (IST)", datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"), inline=False),
        ],
        "🟣 DELTA EXCHANGE INDIA • SUPERTREND SIGNAL • CONFIRMED CANDLE")


def _discord_target_hit(symbol, level, target_price, current_price, quantity, trigger_time, alert_type="TARGET", cycle_key="", cycle_counts=None):
    return _discord_alert(
        f"{alert_type}_{symbol}_L{level}_{trigger_time}",
        f"🎯 {alert_type.replace('_', ' ')} HIT: L{level} {symbol}", 3066993,
        [_discord_field("Status", f"L{level} HIT"),
         _discord_field("Target Price", f"{_discord_exact(target_price)}"),
         _discord_field("Current Market Price", f"{_discord_exact(current_price)}"),
         _discord_field("Quantity Executed", f"{quantity} contracts"),
         *_discord_cycle_count_fields(cycle_key, cycle_counts or {}),
         _discord_field("Trigger Time", trigger_time),
         _discord_field("Alert Time", datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"))],
        "Trading Bot • Target Hit Alert")


def _discord_reentry(symbol, action, level, price, quantity, candle_time, cycle_key="", cycle_counts=None):
    action = str(action).upper()
    return _discord_alert(
        f"REENTRY_{symbol}_{action}_L{level}_{candle_time}",
        f"🔄 RE-ENTRY ALERT: {action} {symbol}", 3066993 if action == "BUY" else 15158332,
        [_discord_field("Action", f"{action} RE-ENTRY"), _discord_field("Level", f"L-{level}"),
         _discord_field("Position Status", "OPEN / FILLED"),
         _discord_field("Re-Entry Price", f"{_discord_exact(price)}"),
         _discord_field("Quantity", f"{quantity} contracts"),
         *_discord_cycle_count_fields(cycle_key, cycle_counts or {}),
         _discord_field("Candle Time", candle_time)],
        "Trading Bot • Re-Entry Alert")


# ============================================================
# SETTINGS
# ============================================================

# ============================================================
# REAL PRODUCTION ENVIRONMENT ONLY — no production switch.
# Use REAL_OWNER_API_KEY and REAL_OWNER_API_SECRET in Secrets/env.
# ============================================================
TRADING_MODE = "REAL"
BASE_URL = "https://api.india.delta.exchange"
PUBLIC_WS_URL = "wss://public-socket.india.delta.exchange"
PUBLIC_REST_V2_URL = BASE_URL + "/v2"

def _read_secret(name, default=""):
    """Read a credential from Streamlit Secrets first, then environment."""
    try:
        value = st.secrets.get(name, default)
    except Exception:
        value = os.getenv(name, default)
    if value in (None, ""):
        value = os.getenv(name, default)
    return str(value or "")


def _load_owner_credentials():
    """Load production credentials only; never fall back to other keys."""
    return _read_secret("REAL_OWNER_API_KEY"), _read_secret("REAL_OWNER_API_SECRET")

SYMBOL = "XRPUSD"  # XRPUSD-only dashboard; deliberately not configurable to BTCUSD
PRODUCT_ID = int(os.getenv("DELTA_PRODUCT_ID", "-1"))
PRODUCT_VERIFIED = False
PRODUCT_DETAILS = {}
PRODUCT_ERROR = f"{TRADING_MODE} XRPUSD product has not been verified yet."

# ------------------------------------------------------------
# SUPERTREND REMOTE CONTROL DEFAULTS
# ------------------------------------------------------------
# These are defaults only. The live dashboard controls below can
# change timeframe / ATR period / multiplier without editing this file.
TIMEFRAME = "1h"
ATR_PERIOD = 10
MULTIPLIER = 2.0

_TIMEFRAME_SECONDS = {"1h": 3600}
CANDLE_SECONDS = _TIMEFRAME_SECONDS.get(TIMEFRAME, 3600)

REFRESH_SECONDS = 5

# Mode flags are derived from the one switch above; do not edit separately.
REAL_MARKET_ONLY = True

# ============================================================
# DEFAULT REMOTE CONTROL SETTINGS
# ============================================================

DEFAULT_BUY_OFFSET = int(
    os.getenv("BUY_OFFSET", "0")
)

DEFAULT_SELL_OFFSET = int(
    os.getenv("SELL_OFFSET", "0")
)

# ============================================================
# GRID QUANTITY CONTROL
# This setting is visible in the dashboard and can be changed
# without editing the Python file.
# 1 XRP = 1 contract per grid order
# 2 XRP = 2 contracts per grid order
# 3 XRP = 3 contracts per grid order
# ============================================================
CONTRACT_XRP = 1.0

# ============================================================
# OWNER ACCOUNT — SINGLE ACCOUNT SETTINGS
# ============================================================
OWNER_DEFAULT_QTY = 5.0  # XRPUSD: 5 contracts = 5 XRP by default
if OWNER_DEFAULT_QTY <= 0:
    OWNER_DEFAULT_QTY = 1.0

# Quantity is computed from Delta India's real available USD balance and persisted signal state.
# Do not expose a manual override that could bypass the balance-based rule.
OWNER_GRID_QTY = float(OWNER_DEFAULT_QTY)
ACCOUNT_VIEW = "OWNER ACCOUNT"

def _qty_to_contracts(qty_btc):
    total = float(qty_btc) / CONTRACT_XRP
    if abs(total - round(total)) > 1e-9:
        raise ValueError("XRPUSD GRID QUANTITY must be a whole number of XRP contracts (1 XRP per contract)")
    return int(round(total))

OWNER_ORDER_SIZE = _qty_to_contracts(OWNER_GRID_QTY)

# ============================================================
# OWNER ACCOUNT DIRECTION REMOTE CONTROL
# ============================================================
_DIRECTION_OPTIONS = ["SUPER TREND", "OPPOSITE", "BUY", "SELL"]

OWNER_DIRECTION_MODE = st.selectbox(
    "🎛️ OWNER ACCOUNT — TRADING DIRECTION",
    _DIRECTION_OPTIONS,
    index=_DIRECTION_OPTIONS.index("SUPER TREND"),
    key="owner_direction_mode",
)

OWNER_EFFECTIVE_DIRECTION = ""

def _resolve_account_direction(mode, supertrend_direction):
    mode = str(mode or "SUPER TREND").upper().strip()
    st_dir = str(supertrend_direction or "").upper().strip()
    if mode == "BUY":
        return "BUY"
    if mode == "SELL":
        return "SELL"
    if mode == "OPPOSITE":
        return "SELL" if st_dir == "BUY" else "BUY" if st_dir == "SELL" else ""
    return st_dir

# Backward-compatible names used by the existing engine.
ORDER_QTY = float(OWNER_GRID_QTY)
DEFAULT_ORDER_SIZE = int(OWNER_ORDER_SIZE)

# LIMIT pending रहने के बाद कितने seconds में MARKET करना है.
# 0 = automatic MARKET conversion बंद.
DEFAULT_LIMIT_TIMEOUT = int(
    os.getenv("LIMIT_TIMEOUT", "6000000")
)

TARGET_1 = float(os.getenv("XRP_TARGET_1", "0.0000"))
TARGET_2 = float(os.getenv("XRP_TARGET_2", "0.0040"))
TARGET_3 = float(os.getenv("XRP_TARGET_3", "0.0100"))
TARGET_4 = float(os.getenv("XRP_TARGET_4", "0.0150"))
TARGET_5 = float(os.getenv("XRP_TARGET_5", "0.0200"))
TARGET_6 = float(os.getenv("XRP_TARGET_6", "0.0250"))
TARGET_7 = float(os.getenv("XRP_TARGET_7", "0.0300"))
TARGET_8 = float(os.getenv("XRP_TARGET_8", "0.0350"))
TARGET_9 = float(os.getenv("XRP_TARGET_9", "0.0400"))
TARGET_10 = float(os.getenv("XRP_TARGET_10", "0.0450"))
TARGET_11 = float(os.getenv("XRP_TARGET_11", "0.0500"))
TARGET_12 = float(os.getenv("XRP_TARGET_12", "0.0550"))
TARGET_13 = float(os.getenv("XRP_TARGET_13", "0.0600"))
TARGET_14 = float(os.getenv("XRP_TARGET_14", "0.0650"))
TARGET_15 = float(os.getenv("XRP_TARGET_15", "0.0700"))
TARGET_16 = float(os.getenv("XRP_TARGET_16", "0.0750"))
TARGET_17 = float(os.getenv("XRP_TARGET_17", "0.0800"))

# ============================================================
# PAGE
# ============================================================

st.set_page_config(
    page_title="Sanjay Rana XRPUSD Real Trading",
    page_icon="📈",
    layout="wide"
)

# ============================================================
# SUPERTREND REMOTE CONTROL — MAIN DASHBOARD (VISIBLE)
# ============================================================
st.markdown(
    """
    <div style="margin-top:10px;padding:14px 16px;border-radius:14px;
    background:linear-gradient(135deg,#0f172a,#172554);
    border:2px solid #38bdf8;box-shadow:0 0 16px rgba(56,189,248,.18);">
      <div style="font-size:22px;font-weight:950;color:#38bdf8;">🎛️ SUPERTREND REMOTE CONTROL</div>
      <div style="margin-top:4px;font-size:13px;font-weight:700;color:#cbd5e1;">
        Timeframe / ATR / Multiplier यहाँ से बदलें — बदलते ही SuperTrend, Signal, Grid Anchor और History उसी setting पर recalculate होंगे.
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Fixed single SuperTrend configuration: 1H / ATR 10 / Multiplier 2.0 / HL2.
TIMEFRAME = "1h"
ATR_PERIOD = 10
MULTIPLIER = 2.0
CANDLE_SECONDS = 3600

st.markdown(
    "<div style='margin:6px 0 14px 0;padding:9px 12px;border-radius:9px;background:#111827;border:1px solid #334155;color:#e2e8f0;font-weight:900;'>🎯 ACTIVE SUPERTREND: <span style='color:#22c55e;'>1H</span> &nbsp;|&nbsp; ATR <span style='color:#38bdf8;'>10</span> &nbsp;|&nbsp; MULTIPLIER <span style='color:#facc15;'>2.0</span> &nbsp;|&nbsp; HL2 &nbsp;|&nbsp; Confirmed Candle Close</div>",
    unsafe_allow_html=True,
)


st.markdown(
    f"""
    <style>
    .sr-banner {{
        position: relative; width: 100%; overflow: hidden;
        border-radius: 12px; margin: 0 0 16px 0;
    }}
    .sr-banner img {{display:block;width:100%;height:auto;}}
    .sr-banner-title {{
        position:absolute; top:2%; left:50%; transform:translateX(-50%);
        width:max-content; max-width:96%; text-align:center;
        font-size:clamp(15px, 2.5vw, 39px); font-weight:900;
        color:#ffffff; text-shadow:0 2px 7px #000, 0 0 12px #000;
        background:rgba(0,0,0,.72); border-radius:9px;
        padding:8px 15px; letter-spacing:.5px;
    }}
    @media(max-width:600px) {{
        .sr-banner-title {{font-size:clamp(10px,3.15vw,18px);padding:5px 7px;top:1%;}}
    }}
    </style>
    <div class="sr-banner">
      <div class="sr-banner-title">SANJAY RANA REAL DASHBOARD GRID BOT</div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ------------------------------------------------------------
# Removed decorative SANJAY RANA / animated SuperTrend BUY-SELL panel (not real market data).

st.caption("SUPERTREND FIXED SETTING: 1H | ATR 10 | MULTIPLIER 2.0 | HL2 | CONFIRMED CANDLE CLOSE")


# ============================================================
# INDIAN TIME
# ============================================================

IST = timezone(timedelta(hours=5, minutes=30))


def indian_time(timestamp):
    try:
        ts = int(float(timestamp))

        if ts > 10_000_000_000:
            ts = ts // 1000

        return datetime.fromtimestamp(
            ts,
            tz=timezone.utc
        ).astimezone(IST).strftime(
            "%Y-%m-%d %H:%M:%S IST"
        )

    except Exception:
        return "-"


# ============================================================
# DISPLAY HELPERS
# ============================================================

def number(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def show_price(value):
    value = number(value)

    if value is None:
        return "-"

    return f"{value:,.6f}"


def _grid_log_event(account_id, level_no, role, message, price=None):
    if "grid_error_log" not in st.session_state:
        st.session_state["grid_error_log"] = []
    st.session_state["grid_error_log"].append({
        "time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "account": account_id,
        "level": f"L{level_no}" if level_no else "-",
        "role": role,
        "price": price,
        "message": str(message),
    })
    st.session_state["grid_error_log"] = st.session_state["grid_error_log"][-300:]


# ============================================================
# ADDITIVE ORDER LIFECYCLE TRACKING
# Existing trading logic is intentionally left unchanged.
# ============================================================

def _order_epoch(value):
    if value is None:
        return None
    try:
        ts = float(value)
        if ts > 10_000_000_000:
            ts /= 1000.0
        return ts
    except Exception:
        return None


def _order_event_epoch(order, keys):
    for key in keys:
        if key in order and order.get(key) not in [None, ""]:
            ts = _order_epoch(order.get(key))
            if ts is not None:
                return ts
    return None


def _format_epoch(ts):
    if ts is None:
        return "-"
    return indian_time(ts)


def _duration_text(seconds):
    if seconds is None:
        return "-"
    try:
        seconds = max(0, int(seconds))
    except Exception:
        return "-"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m {secs}s"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _ensure_order_tracking():
    if "order_tracking" not in st.session_state:
        st.session_state["order_tracking"] = {}
    return st.session_state["order_tracking"]


def _remember_sent_order(order_id, data):
    if order_id is None:
        return
    tracking = _ensure_order_tracking()
    tracking[str(order_id)] = dict(data)


def _order_status_label(order):
    state = str(order.get("state", "")).lower()
    if state in {"filled", "closed"}:
        return "EXECUTED / CLOSED"
    if state in {"cancelled", "canceled"}:
        return "CANCELLED"
    if state in {"rejected", "failed"}:
        return "REJECTED"
    if state in {"open", "pending", "active", "partially_filled"}:
        return "PENDING"
    return str(order.get("state", "-")).upper()


def _order_sent_epoch(order):
    return _order_event_epoch(
        order,
        (
            "created_at_ts",
            "created_at",
            "created_time",
            "timestamp"
        )
    )


def _order_final_epoch(order):
    return _order_event_epoch(
        order,
        (
            "filled_at",
            "executed_at",
            "closed_at",
            "cancelled_at",
            "canceled_at",
            "updated_at"
        )
    )


# ============================================================
# DELTA API
# ============================================================

class DeltaAPI:

    def __init__(self, api_key=None, api_secret=None):
        # Credentials belong to this client instance. Do not mutate module-level
        # credentials when a Member status check is performed.
        self.api_key = str(api_key if api_key is not None else globals().get("API_KEY", "") or "").strip()
        self.api_secret = str(api_secret if api_secret is not None else globals().get("API_SECRET", "") or "").strip()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Sanjay-Rana-Real-Trading-Bot",
            "Accept": "application/json"
        })

    def _redact_text(self, value):
        """Remove this client's credentials from any returned/logged error text."""
        text = str(value)
        for secret_value in (self.api_key, self.api_secret):
            if secret_value:
                text = text.replace(secret_value, "[REDACTED]")
        return text

    def _redact_payload(self, value):
        if isinstance(value, dict):
            return {key: self._redact_payload(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_payload(item) for item in value]
        if isinstance(value, str):
            return self._redact_text(value)
        return value

    # --------------------------------------------------------
    # HMAC SIGNATURE
    # --------------------------------------------------------
    def make_signature(self, method, timestamp, path, query_string="", body=""):
        message = method.upper() + str(timestamp) + path + query_string + body
        return hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    # --------------------------------------------------------
    # REQUEST
    # --------------------------------------------------------
    def request(self, method, path, params=None, body=None, private=False):
        params = params or {}
        body = body or {}
        payload = json.dumps(body, separators=(",", ":")) if body else ""
        headers = {
            "Accept": "application/json",
            "User-Agent": "Sanjay-Rana-Real-Trading-Bot"
        }

        # Sign the same encoded query string that requests will put on the URL.
        query_string = ""
        if params:
            prepared = requests.Request(
                method.upper(), BASE_URL + path, params=params
            ).prepare()
            encoded_query = urlsplit(prepared.url).query
            if encoded_query:
                query_string = "?" + encoded_query

        if private:
            if not self.api_key or not self.api_secret:
                return {
                    "success": False,
                    "error": {"code": "missing_credentials", "message": "API credentials are not configured."}
                }
            timestamp = str(int(time.time()))
            signature = self.make_signature(method, timestamp, path, query_string, payload)
            headers.update({
                "api-key": self.api_key,
                "timestamp": timestamp,
                "signature": signature,
                "Content-Type": "application/json"
            })

        try:
            response = self.session.request(
                method.upper(), BASE_URL + path,
                params=params,
                data=payload if payload else None,
                headers=headers,
                timeout=15
            )
            try:
                data = response.json()
            except Exception:
                return {
                    "success": False,
                    "error": {
                        "code": "non_json_response",
                        "message": f"Delta API returned a non-JSON response (HTTP {response.status_code})."
                    }
                }

            # A non-2xx HTTP status must never count as a successful connection.
            if not response.ok:
                if isinstance(data, dict):
                    data = dict(data)
                    data["success"] = False
                    data.setdefault("error", {
                        "code": f"http_{response.status_code}",
                        "message": f"Delta API returned HTTP {response.status_code}."
                    })
                else:
                    data = {
                        "success": False,
                        "error": {
                            "code": f"http_{response.status_code}",
                            "message": f"Delta API returned HTTP {response.status_code}."
                        }
                    }
            return self._redact_payload(data)
        except requests.exceptions.Timeout:
            return {"success": False, "error": {"code": "timeout", "message": "Delta API request timed out."}}
        except requests.exceptions.RequestException as exc:
            return {"success": False, "error": {"code": "network_error", "message": self._redact_text(type(exc).__name__)}}
        except Exception as exc:
            return {"success": False, "error": {"code": "request_error", "message": self._redact_text(type(exc).__name__)}}

    # --------------------------------------------------------
    # PUBLIC
    # --------------------------------------------------------

    def candles(self):

        # Keep the existing candle source/logic, but fetch enough completed
        # Completed 1H candles for the fixed SuperTrend 10,2 calculation and chart.
        end = int(time.time())
        chunk = 500 * CANDLE_SECONDS
        all_rows = []

        for _n in range(4):
            start = end - chunk
            response = self.request(
                "GET",
                "/v2/history/candles",
                params={
                    "symbol": SYMBOL,
                    "resolution": TIMEFRAME,
                    "start": start,
                    "end": end
                }
            )
            if not isinstance(response, dict) or not response.get("success"):
                if all_rows:
                    break
                return response
            result = response.get("result")
            if isinstance(result, list):
                all_rows.extend(result)
            end = start

        return {
            "success": True,
            "result": all_rows
        }


    def ticker(self):

        return self.request(
            "GET",
            f"/v2/tickers/{SYMBOL}"
        )


    # --------------------------------------------------------
    # PRIVATE
    # --------------------------------------------------------

    def open_orders(self):

        if not PRODUCT_VERIFIED:
            return {"success": False, "error": {"code": "product_unverified", "message": PRODUCT_ERROR}}

        # FIX: Delta's /v2/orders endpoint defaults to page_size=10 when no
        # page_size is sent. The previous implementation never sent page_size
        # and never followed the "after" cursor, so only the first 10 open
        # orders were ever returned here — even though many more orders were
        # genuinely active on the exchange order book. This caused upper/later-created
        # levels to silently disappear from the dashboard while still being
        # fully live on the exchange order book.
        rows = []
        cursor = None
        seen_cursors = set()
        last_response = None

        for _page in range(10):
            params = {
                "product_id": PRODUCT_ID,
                "state": "open",
                "page_size": 100,
            }
            if cursor:
                params["after"] = cursor

            response = self.request(
                "GET",
                "/v2/orders",
                params=params,
                private=True
            )
            last_response = response

            if not isinstance(response, dict) or response.get("success") is not True:
                if rows:
                    break
                return response if isinstance(response, dict) else {
                    "success": False,
                    "error": "Invalid open orders response"
                }

            result = response.get("result", [])
            if isinstance(result, list):
                rows.extend(item for item in result if isinstance(item, dict))

            meta = response.get("meta", {})
            next_cursor = meta.get("after") if isinstance(meta, dict) else None
            if not next_cursor or str(next_cursor) in seen_cursors:
                break
            seen_cursors.add(str(next_cursor))
            cursor = str(next_cursor)

        return {
            "success": True,
            "result": rows,
            "meta": {"pages_limit": 10, "rows_loaded": len(rows)}
        }


    def _fetch_product_history_pages(self, path):
        """Fetch up to 5 cursor pages so older fills/orders are not silently missed.

        Delta removed the old `total` metadata field; `meta.after` is the
        cursor used for the next page. This is read-only and never submits or
        cancels an order.
        """
        if not PRODUCT_VERIFIED:
            return {"success": False, "error": {"code": "product_unverified", "message": PRODUCT_ERROR}}

        rows = []
        cursor = None
        seen_cursors = set()
        last_response = None
        for _page in range(5):
            params = {"product_ids": str(PRODUCT_ID), "page_size": 50}
            if cursor:
                params["after"] = cursor
            response = self.request("GET", path, params=params, private=True)
            last_response = response
            if not isinstance(response, dict) or response.get("success") is not True:
                if rows:
                    break
                return response if isinstance(response, dict) else {"success": False, "error": "Invalid history response"}

            result = response.get("result", [])
            if isinstance(result, list):
                rows.extend(item for item in result if isinstance(item, dict))
            elif isinstance(result, dict):
                nested = result.get("orders", result.get("fills", []))
                if isinstance(nested, list):
                    rows.extend(item for item in nested if isinstance(item, dict))

            meta = response.get("meta", {})
            next_cursor = meta.get("after") if isinstance(meta, dict) else None
            if not next_cursor or str(next_cursor) in seen_cursors:
                break
            seen_cursors.add(str(next_cursor))
            cursor = str(next_cursor)

        return {"success": True, "result": rows, "meta": {"pages_limit": 5, "rows_loaded": len(rows)}}


    def closed_orders(self):
        """Fetch recent closed/cancelled order history with cursor pagination."""
        return self._fetch_product_history_pages("/v2/orders/history")


    def fills(self):
        """Fetch real execution records with cursor pagination."""
        return self._fetch_product_history_pages("/v2/fills")


    def get_order_by_client_id(self, client_order_id):
        """Direct per-order lookup; does NOT depend on bulk list/pagination.

        Delta's GET /v2/orders/client_order_id/{client_oid} returns a single
        order's latest state (open/closed/cancelled) regardless of whether it
        appeared yet in bulk open/closed-order pages. Used only as a fallback
        verification step when an instantly-filled marketable LIMIT order
        would otherwise appear "missing" from both feeds.
        """
        if not client_order_id:
            return {"success": False, "error": "client_order_id is required"}
        return self.request(
            "GET",
            f"/v2/orders/client_order_id/{client_order_id}",
            private=True
        )


    def position(self):

        if not PRODUCT_VERIFIED:
            return {"success": False, "error": {"code": "product_unverified", "message": PRODUCT_ERROR}}


        return self.request(
            "GET",
            "/v2/positions",
            params={
                "product_id": PRODUCT_ID
            },
            private=True
        )


    # --------------------------------------------------------
    # real account BALANCE / MARGIN / LEVERAGE
    # production account snapshot methods
    # --------------------------------------------------------

    def wallet_balances(self):

        return self.request(
            "GET",
            "/v2/wallet/balances",
            private=True
        )


    def order_leverage(self):

        if not PRODUCT_VERIFIED:
            return {"success": False, "error": {"code": "product_unverified", "message": PRODUCT_ERROR}}


        return self.request(
            "GET",
            f"/v2/products/{PRODUCT_ID}/orders/leverage",
            private=True
        )


    def margined_positions(self):

        if not PRODUCT_VERIFIED:
            return {"success": False, "error": {"code": "product_unverified", "message": PRODUCT_ERROR}}


        return self.request(
            "GET",
            "/v2/positions/margined",
            params={
                "product_id": PRODUCT_ID
            },
            private=True
        )


    # --------------------------------------------------------
    # PLACE LIMIT ORDER
    # --------------------------------------------------------

    def place_market_order(self, side, size, client_order_id=None):
        if not PRODUCT_VERIFIED:
            return {"success": False, "error": {"code": "product_unverified", "message": PRODUCT_ERROR}}

        body = {
            "product_id": PRODUCT_ID,
            "product_symbol": SYMBOL,
            "size": int(abs(size)),
            "side": side,
            "order_type": "market_order",
            "reduce_only": False,
        }

        if client_order_id:
            body["client_order_id"] = str(client_order_id)[:32]

        return self.request(
            "POST",
            "/v2/orders",
            body=body,
            private=True
        )


    def place_limit_order(
        self,
        side,
        size,
        limit_price,
        take_profit_price=None,
        client_order_id=None,
        reduce_only=False
    ):
        if not PRODUCT_VERIFIED:
            return {"success": False, "error": {"code": "product_unverified", "message": PRODUCT_ERROR}}

        body = {

            "product_id": PRODUCT_ID,

            "product_symbol": SYMBOL,

            "limit_price": str(
                limit_price
            ),

            "size": int(size),

            "side": side,

            "order_type": "limit_order",

            "time_in_force": "gtc",

            "post_only": False,

            # Delta REST v2 defines reduce_only as a JSON boolean. Sending the
            # string "true" can be rejected or ignored by strict validators,
            # which leaves the target absent on the exchange.
            "reduce_only": bool(reduce_only)
        }

        # Delta supports a bracket take-profit attached directly to a
        # new LIMIT order. This keeps TP visible with the pending entry
        # instead of waiting for a separate TP order after the fill.
        if False and take_profit_price is not None:
            body["bracket_take_profit_price"] = str(
                take_profit_price
            )
            body["bracket_take_profit_limit_price"] = str(
                take_profit_price
            )
            body["bracket_stop_trigger_method"] = (
                "last_traded_price"
            )

        if client_order_id:
            body["client_order_id"] = str(
                client_order_id
            )

        return self.request(
            "POST",
            "/v2/orders",
            body=body,
            private=True
        )


    # --------------------------------------------------------
    # MARKET REDUCE-ONLY ORDER — CLOSE OPPOSITE POSITION
    # --------------------------------------------------------

    def place_market_reduce_only(self, side, size, client_order_id=None):
        if not PRODUCT_VERIFIED:
            return {"success": False, "error": {"code": "product_unverified", "message": PRODUCT_ERROR}}

        body = {
            "product_id": PRODUCT_ID,
            "product_symbol": SYMBOL,
            "size": int(abs(size)),
            "side": side,
            "order_type": "market_order",
            "reduce_only": True,
        }

        if client_order_id:
            body["client_order_id"] = str(client_order_id)[:32]

        return self.request(
            "POST",
            "/v2/orders",
            body=body,
            private=True
        )


    # --------------------------------------------------------
    # CANCEL ORDER
    # --------------------------------------------------------

    def cancel_order(self, order_id):
        # Fail closed: never send a cancellation request with an unknown
        # or unverified non-production product ID.
        if not PRODUCT_VERIFIED or PRODUCT_ID <= 0:
            return {"success": False, "error": {"code": "product_unverified", "message": PRODUCT_ERROR}}

        # Delta Exchange India REST v2 cancellation uses DELETE /v2/orders
        # with the order id/product id in the request body.  Keep this as
        # the canonical single-order cancellation path so manual orders
        # created outside this bot can also be cancelled reliably.
        return self.request(
            "DELETE",
            "/v2/orders",
            body={
                "id": int(order_id),
                "product_id": PRODUCT_ID,
            },
            private=True
        )


    def cancel_all_open_orders_for_product(self):
        # Fail closed: do not cancel against an unknown/invalid product ID.
        if not PRODUCT_VERIFIED or PRODUCT_ID <= 0:
            return {"success": False, "error": {"code": "product_unverified", "message": PRODUCT_ERROR}}

        # Delta Exchange India REST v2 supports account-local cancellation
        # for one product.  All three filters are enabled deliberately so
        # reversal cleanup includes limit, stop/trigger and reduce-only
        # active orders belonging to THIS API account/product.
        return self.request(
            "DELETE",
            "/v2/orders/all",
            body={
                "product_id": PRODUCT_ID,
                "cancel_limit_orders": True,
                "cancel_stop_orders": True,
                "cancel_reduce_only_orders": True,
            },
            private=True
        )


# ============================================================
# API CREDENTIALS (OWNER ONLY FROM GITHUB SECRETS)
# ============================================================
# यह कोड GitHub Secrets से कीज़ खुद ले लेता है
OWNER_API_KEY, OWNER_API_SECRET = _load_owner_credentials()

OWNER_KEY = OWNER_API_KEY
OWNER_SECRET = OWNER_API_SECRET




API_KEY = OWNER_KEY
API_SECRET = OWNER_SECRET


def _resolve_exchange_product(symbol=SYMBOL):
    """Resolve the symbol from the selected exchange catalog; never reuse IDs across modes."""
    try:
        response = requests.get(
            f"{BASE_URL}/v2/products",
            params={"symbol": symbol},
            headers={"Accept": "application/json"},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("success"):
            return -1, False, {}, f"Exchange product lookup failed: {payload}"

        result = payload.get("result", [])
        if isinstance(result, dict):
            products = [result]
        elif isinstance(result, list):
            products = result
        else:
            products = []

        product = next(
            (item for item in products
             if isinstance(item, dict)
             and str(item.get("symbol", "")).upper() == str(symbol).upper()),
            None,
        )
        if not product:
            return -1, False, {}, f"Symbol {symbol} was not found in the exchange product catalog."

        product_id = product.get("id")
        if product_id is None:
            return -1, False, product, "Exchange product response did not contain an ID."

        state = str(product.get("state", "")).lower()
        trading_status = str(product.get("trading_status", "")).lower()
        # Delta product payloads commonly use state=live. If trading_status
        # is present, reject explicit non-operational statuses as well.
        if state != "live":
            return int(product_id), False, product, f"Exchange contract state is '{state or 'unknown'}', not 'live'."
        if trading_status and trading_status not in {"operational", "live", "trading"}:
            return int(product_id), False, product, f"Exchange trading status is '{trading_status}', not operational."

        return int(product_id), True, product, ""
    except Exception as exc:
        return -1, False, {}, f"Exchange product lookup error: {exc}"


PRODUCT_ID, PRODUCT_VERIFIED, PRODUCT_DETAILS, PRODUCT_ERROR = _resolve_exchange_product(SYMBOL)

api = DeltaAPI()



# ============================================================
if True:
    # BASIC STATUS (real / non-production ONLY)
    # ============================================================

    st.info(
        "🔴 REAL LIVE MODE — orders are routed to Delta Exchange India production."
    )
    if PRODUCT_VERIFIED:
        st.success(
            f"✅ REAL product verified: {SYMBOL} • Product ID {PRODUCT_ID} • State: {PRODUCT_DETAILS.get('state', 'live')}"
        )
    else:
        st.error(
            f"🛑 {TRADING_MODE} product NOT verified: {PRODUCT_ERROR} New orders are blocked until the contract is verified."
        )

        # ============================================================
    # OWNER API CONTROL
    # PLACE THIS DIRECTLY BELOW PART 1
    # ============================================================

    st.divider()

    # ============================================================
    # OWNER API
    # ============================================================

    st.header("👑 OWNER API")

    # ============================================================
# STREAMLIT CLOUD PUBLIC IP STATUS
# ============================================================

    # OWNER ACCOUNT STATUS — live private API, wallet balances, host IP
    with st.expander("👑 OWNER ACCOUNT STATUS — विवरण देखने के लिए टैप करें", expanded=False):
        st.subheader("👑 OWNER ACCOUNT STATUS")

        STREAMLIT_PUBLIC_IP = None
        try:
            _ip_response = requests.get("https://api.ipify.org", timeout=5)
            _ip_response.raise_for_status()
            _candidate_ip = _ip_response.text.strip()
            # Validate that the response is an IP address rather than arbitrary text.
            import ipaddress as _ipaddress
            STREAMLIT_PUBLIC_IP = str(_ipaddress.ip_address(_candidate_ip))
        except Exception:
            STREAMLIT_PUBLIC_IP = None

        _owner_api_status = "UNKNOWN"
        _owner_api_error = ""
        _owner_wallet_result = None
        _owner_balance_rows = []
        _owner_last_success = st.session_state.get("owner_last_successful_api_check", "-")
        _owner_whitelist_status = "UNKNOWN"

        OWNER_API_KEY, OWNER_API_SECRET = _load_owner_credentials()
        if not OWNER_API_KEY or not OWNER_API_SECRET:
            _owner_api_status = "DISCONNECTED"
            _owner_api_error = "Owner API credentials missing in the Streamlit hosting environment."
        else:
            try:
                owner_client = DeltaAPI(OWNER_API_KEY, OWNER_API_SECRET)
                _owner_wallet_result = owner_client.wallet_balances()
                if isinstance(_owner_wallet_result, dict) and _owner_wallet_result.get("success") is True:
                    _owner_api_status = "CONNECTED"
                    _owner_whitelist_status = "ACCEPTED / VERIFIED (authenticated private request succeeded)"
                    _owner_last_success = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
                    st.session_state["owner_last_successful_api_check"] = _owner_last_success
                    st.session_state["owner_api_connected"] = True
                    _wallet_payload = _owner_wallet_result.get("result")
                    if isinstance(_wallet_payload, dict):
                        # Some API response variants wrap balances in a "balances" list.
                        _wallet_payload = _wallet_payload.get("balances", [_wallet_payload])
                    if isinstance(_wallet_payload, list):
                        _owner_balance_rows = [row for row in _wallet_payload if isinstance(row, dict)]
                else:
                    st.session_state["owner_api_connected"] = False
                    _err = _owner_wallet_result.get("error", {}) if isinstance(_owner_wallet_result, dict) else {}
                    if isinstance(_err, dict):
                        _err_code = str(_err.get("code", ""))
                        _err_msg = str(_err.get("message", ""))
                    else:
                        _err_code, _err_msg = "", str(_err)
                    _owner_api_error = (f"{_err_code}: {_err_msg}".strip(": ")) or "Delta did not confirm authenticated private API access."
                    _owner_error_lower = _owner_api_error.lower()
                    if any(term in _owner_error_lower for term in ("ip whitelist", "ip_whitelist", "ip restriction", "whitelist", "ip not allowed", "not allowed from this ip")):
                        _owner_whitelist_status = "IP RESTRICTION DETECTED"
                    else:
                        _owner_whitelist_status = "UNKNOWN"
            except Exception as _owner_exc:
                st.session_state["owner_api_connected"] = False
                _owner_api_error = f"Connection check failed ({type(_owner_exc).__name__}); sensitive details hidden."
                _owner_whitelist_status = "UNKNOWN"

        # Render the actual settlement wallet row: prefer USDT if present; otherwise
        # show Delta's actual USD wallet as USD (never mislabel USD as USDT).
        _balance_values_shown = False
        def _wallet_currency(_row):
            return str(
                _row.get("asset_symbol") or _row.get("currency")
                or _row.get("asset") or _row.get("symbol") or ""
            ).upper().strip()

        _balance_currency = "USDT"
        _balance_row = next((row for row in _owner_balance_rows if _wallet_currency(row) == "USDT"), None)
        if _balance_row is None:
            _balance_row = next((row for row in _owner_balance_rows if _wallet_currency(row) == "USD"), None)
            _balance_currency = "USD"

        if _balance_row is not None:
            _total = next((_balance_row.get(k) for k in ("balance", "total_balance", "total", "wallet_balance") if _balance_row.get(k) is not None), None)
            _available = next((_balance_row.get(k) for k in ("available_balance", "available", "available_to_withdraw", "available_balance_for_trade") if _balance_row.get(k) is not None), None)
            _reserved = next((_balance_row.get(k) for k in ("reserved_balance", "reserved", "locked_balance", "order_margin") if _balance_row.get(k) is not None), None)
            if any(v is not None for v in (_total, _available, _reserved)):
                _balance_values_shown = True
                st.caption(f"एक्सचेंज वॉलेट का वास्तविक बैलेंस — {_balance_currency}")
                _bc = st.columns(3)
                for _col, _label, _value in (
                    (_bc[0], f"कुल बैलेंस ({_balance_currency})", _total),
                    (_bc[1], f"उपलब्ध बैलेंस ({_balance_currency})", _available),
                    (_bc[2], f"आरक्षित बैलेंस ({_balance_currency})", _reserved),
                ):
                    with _col:
                        if _value is None:
                            st.metric(_label, "API में उपलब्ध नहीं")
                        else:
                            _numeric_value = number(_value)
                            st.metric(_label, f"{_numeric_value:,.8f} {_balance_currency}" if _numeric_value is not None else "API मान अमान्य")
        # Leverage is shown only when returned by the authenticated exchange API.
        _owner_leverage_text = "API से सत्यापित मान उपलब्ध नहीं"
        try:
            _lev_client = DeltaAPI(OWNER_API_KEY, OWNER_API_SECRET)
            _lev_response = _lev_client.order_leverage() if PRODUCT_VERIFIED else {"success": False}
            if isinstance(_lev_response, dict) and _lev_response.get("success") is True:
                _lev_payload = _lev_response.get("result", _lev_response)
                _lev_value = None
                def _find_leverage_value(_obj):
                    if isinstance(_obj, dict):
                        for _key in ("leverage", "order_leverage", "leverage_value"):
                            if _obj.get(_key) not in (None, ""):
                                return _obj.get(_key)
                        for _nested in _obj.values():
                            _found = _find_leverage_value(_nested)
                            if _found is not None:
                                return _found
                    elif isinstance(_obj, list):
                        for _nested in _obj:
                            _found = _find_leverage_value(_nested)
                            if _found is not None:
                                return _found
                    return None
                _lev_value = _find_leverage_value(_lev_payload)
                if _lev_value is not None:
                    _lev_number = float(_lev_value)
                    if math.isfinite(_lev_number) and _lev_number > 0:
                        _owner_leverage_text = f"{_lev_number:g}×"
        except Exception:
            pass
        st.metric("एक्सचेंज Leverage", _owner_leverage_text)


        if _owner_api_status == "CONNECTED":
            st.success("🟢 DELTA API CONNECTION: CONNECTED — authenticated private wallet request succeeded.")
        else:
            st.error("🔴 DELTA API CONNECTION: DISCONNECTED" if _owner_api_status == "DISCONNECTED" else "🟡 DELTA API CONNECTION: UNKNOWN")
        if not _balance_values_shown:
            st.info("USDT बैलेंस API में नहीं मिला। किसी अन्य मुद्रा (USD/INR/BTC/XRP आदि) को USDT मानकर नहीं दिखाया जाएगा।")
        if STREAMLIT_PUBLIC_IP:
            st.write(f"🌐 **SERVER PUBLIC IP:** `{STREAMLIT_PUBLIC_IP}`")
        else:
            st.warning("🌐 **SERVER PUBLIC IP: IP UNAVAILABLE**")

        if _owner_whitelist_status == "ACCEPTED / VERIFIED (authenticated private request succeeded)":
            st.success("🟢 IP WHITELIST STATUS: Accepted / verified by successful authenticated private request.")
        elif _owner_whitelist_status == "IP RESTRICTION DETECTED":
            st.error("🔴 IP WHITELIST STATUS: IP restriction detected in the Delta API error.")
        else:
            st.warning("🟡 IP WHITELIST STATUS: Unknown — Delta's response did not confirm an IP restriction or whitelist acceptance.")

        st.write(f"**Last Successful API Check:** {_owner_last_success}")
        # Sanitize before any error text is rendered.
        _safe_display_error = str(_owner_api_error or "None on the latest check")
        for _secret_value in (OWNER_API_KEY, OWNER_API_SECRET):
            if _secret_value:
                _safe_display_error = _safe_display_error.replace(_secret_value, "[REDACTED]")
        st.write(f"**Last API Error:** {_safe_display_error}")

        # Never persist API credentials in Streamlit session_state.
        st.session_state.pop("owner_api_key", None)
        st.session_state.pop("owner_api_secret", None)


        # ============================================================
        # FIVE MEMBER API CONNECTION STATUS — MONITORING ONLY
        # Configure real_MEMBER1_API_KEY / real_MEMBER1_API_SECRET ... MEMBER5
        # or REAL_MEMBER1_API_KEY / REAL_MEMBER1_API_SECRET ... MEMBER5
        # This block does NOT place, cancel, or route any trading orders.
        # ============================================================
    st.divider()
    with st.expander("👥 MEMBER DETAILS — सभी Member की जानकारी देखने के लिए टैप करें", expanded=False):
        st.subheader("👥 MEMBER DETAILS — टैप करके खोलें")
        st.caption(
            "हर Member का API connection अलग-अलग verify होता है। "
            "यह केवल status monitor है; Member accounts पर orders नहीं भेजता।"
        )

        def _load_member_credentials(member_no):
            prefix = f"REAL_MEMBER{member_no}"
            key = _read_secret(f"{prefix}_API_KEY")
            secret = _read_secret(f"{prefix}_API_SECRET")
            # Optional compatibility names for deployments that use MEMBER1_API_KEY.
            if not key:
                key = _read_secret(f"MEMBER{member_no}_API_KEY")
            if not secret:
                secret = _read_secret(f"MEMBER{member_no}_API_SECRET")
            return key, secret

        _member_status_columns = st.columns(2)
        for _member_no in range(1, 6):
            _member_key, _member_secret = _load_member_credentials(_member_no)
            _member_status = "DISCONNECTED"
            _member_reason = ""
            if not _member_key or not _member_secret:
                _member_reason = (
                    f"API key/secret missing: "
                    f"REAL_MEMBER{_member_no}_API_KEY / "
                    f"REAL_MEMBER{_member_no}_API_SECRET"
                )
            else:
                try:
                    # This read-only client keeps its own Member credentials isolated.
                    _member_client = DeltaAPI(_member_key, _member_secret)
                    _member_response = _member_client.wallet_balances()
                    if isinstance(_member_response, dict) and _member_response.get("success") is True:
                        _member_status = "CONNECTED"
                        _member_reason = "API authentication successful"
                    else:
                        _member_error = str(
                            _member_response.get("error", _member_response)
                            if isinstance(_member_response, dict)
                            else _member_response
                        )
                        _member_lower_error = _member_error.lower()
                        if "ip" in _member_lower_error or "whitelist" in _member_lower_error:
                            _member_reason = "IP whitelist/network restriction suspected; check the host public IP in Delta API settings."
                        elif any(x in _member_lower_error for x in ("api key", "signature", "unauthorized", "authentication", "permission", "forbidden")):
                            _member_reason = "API authentication/permission failed; verify the key pair and permissions."
                        else:
                            _member_reason = "Delta did not confirm authenticated access; check host network and API settings."
                except Exception as _member_exc:
                    _member_reason = "Connection check failed; sensitive error details hidden."

            with _member_status_columns[(_member_no - 1) % 2]:
                if _member_status == "CONNECTED":
                    st.success(f"Member {_member_no}: CONNECTED 🟢")
                else:
                    st.error(f"Member {_member_no}: DISCONNECTED 🔴")
                st.caption(_member_reason)

        # Member status clients do not alter Owner credentials; no restoration needed.


    # ============================================================
    # CONNECTION DISCONNECT / RECONNECT TIME TRACKER
    # ============================================================
    # यह केवल connection status का monitoring block है।
    # Existing trading/order logic को नहीं बदलता।

    _now_connection_ist = datetime.now(timezone.utc).astimezone(IST)
    _now_connection_text = _now_connection_ist.strftime("%Y-%m-%d %H:%M:%S IST")

    if "connection_was_connected" not in st.session_state:
        st.session_state["connection_was_connected"] = None

    if "disconnect_started_at" not in st.session_state:
        st.session_state["disconnect_started_at"] = ""

    if "last_reconnected_at" not in st.session_state:
        st.session_state["last_reconnected_at"] = ""

    if "last_connection_check_at" not in st.session_state:
        st.session_state["last_connection_check_at"] = ""

    _current_connection_ok = bool(
        st.session_state.get("owner_api_connected", False)
    )

    if _current_connection_ok:
        # अगर पहले disconnect था और अब connection वापस आया है
        if (
            st.session_state["connection_was_connected"] is False
            and st.session_state["disconnect_started_at"]
        ):
            st.session_state["last_reconnected_at"] = _now_connection_text

        st.session_state["connection_was_connected"] = True
        st.session_state["last_connection_check_at"] = _now_connection_text

    else:
        # पहली बार disconnect detect होने का exact समय
        if st.session_state["connection_was_connected"] is not False:
            st.session_state["disconnect_started_at"] = _now_connection_text

        st.session_state["connection_was_connected"] = False
        st.session_state["last_connection_check_at"] = _now_connection_text

    with st.expander("📡 CONNECTION DISCONNECT MONITOR — विवरण देखने के लिए टैप करें", expanded=False):
        st.divider()
        st.subheader("📡 CONNECTION DISCONNECT MONITOR")

        if _current_connection_ok:
            st.success("🟢 CONNECTION: CONNECTED")

            st.write(
                "Last Successful Connection: "
                f"**{st.session_state['last_connection_check_at']}**"
            )

            if st.session_state["last_reconnected_at"]:
                st.info(
                    "🟢 RECONNECTED AT: "
                    f"**{st.session_state['last_reconnected_at']}**"
                )

                if st.session_state["disconnect_started_at"]:
                    st.write(
                        "Previous Disconnect Started At: "
                        f"**{st.session_state['disconnect_started_at']}**"
                    )
            else:
                st.write(
                    "Disconnect History: **No disconnect detected in this session**"
                )

        else:
            st.error("🔴 CONNECTION: DISCONNECTED")

            st.write(
                "Disconnected At: "
                f"**{st.session_state['disconnect_started_at']}**"
            )

            st.write(
                "Last Connection Check: "
                f"**{st.session_state['last_connection_check_at']}**"
            )


    # ============================================================
# MEMBER API / MEMBER SUMMARY REMOVED
# OWNER API ONLY
# ============================================================

# ============================================================
# END — OWNER API BLOCK
# ============================================================
# CANDLE DATA + SUPERTREND ENGINE
# ============================================================

def get_result(data):

    if not data:
        return None

    if not data.get("success"):
        return None

    return data.get("result")


def make_dataframe(data):

    result = get_result(data)

    if not isinstance(result, list):
        return pd.DataFrame()

    rows = []

    for candle in result:
        try:
            rows.append({
                "time": int(candle["time"]),
                "open": float(candle["open"]),
                "high": float(candle["high"]),
                "low": float(candle["low"]),
                "close": float(candle["close"]),
                "volume": float(candle.get("volume", 0))
            })
        except Exception:
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    df = (
        df
        .drop_duplicates("time")
        .sort_values("time")
        .reset_index(drop=True)
    )

    return df


# ============================================================
# GET CANDLES
# ============================================================

candle_response = api.candles()

df = make_dataframe(candle_response)

if df.empty:
    st.error("Delta se candle data nahi mila.")
    st.stop()


# ============================================================
# ONLY COMPLETED CANDLES FOR THE SELECTED TIMEFRAME
# ============================================================
# Delta candle timestamps are used directly. This avoids hard-coding
# the old 1-hour :30 boundary and works for every remote timeframe.
_now_epoch = int(time.time())
if not df.empty:
    _last_candle_start = int(pd.to_numeric(df["time"].iloc[-1], errors="coerce"))
    if _last_candle_start > 10_000_000_000:
        _last_candle_start //= 1000
    if _last_candle_start + CANDLE_SECONDS > _now_epoch:
        df = df.iloc[:-1].copy()

df = df.reset_index(drop=True)

df = df.reset_index(drop=True)

if len(df) < ATR_PERIOD + 5:
    st.error("SuperTrend ke liye enough candles nahi hain.")
    st.stop()


# ============================================================
# ============================================================
# STANDARD TRADINGVIEW SUPERTREND REMOTE-CONTROL ENGINE
# ============================================================
# Reference chart: regular Delta XRPUSD candles for the selected timeframe,
# SuperTrend uses the selected ATR period/multiplier, Source = HL2.
#
# This is NOT Heikin-Ashi.
# ATR = TradingView-style Wilder/RMA.
# Only completed 1-hour Delta candles reach this engine.
# ============================================================

prev_close = df["close"].shift(1)

tr1 = df["high"] - df["low"]
tr2 = (df["high"] - prev_close).abs()
tr3 = (df["low"] - prev_close).abs()
df["TR"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

df["ATR"] = float("nan")

if len(df) >= ATR_PERIOD:
    df.loc[ATR_PERIOD - 1, "ATR"] = (
        df["TR"].iloc[:ATR_PERIOD].mean()
    )

    for i in range(ATR_PERIOD, len(df)):
        df.loc[i, "ATR"] = (
            df.loc[i - 1, "ATR"] * (ATR_PERIOD - 1)
            + df.loc[i, "TR"]
        ) / ATR_PERIOD

df["HL2"] = (df["high"] + df["low"]) / 2.0

df["BASIC_UPPER"] = float("nan")
df["BASIC_LOWER"] = float("nan")
df["FINAL_UPPER"] = float("nan")
df["FINAL_LOWER"] = float("nan")
df["SUPERTREND"] = float("nan")
df["ST_DIRECTION"] = 0
df["SIGNAL"] = ""

prev_final_upper = None
prev_final_lower = None
prev_supertrend = None
prev_direction = 1

for i in range(len(df)):
    atr = df.loc[i, "ATR"]

    if pd.isna(atr):
        continue

    hl2 = float(df.loc[i, "HL2"])
    close = float(df.loc[i, "close"])

    basic_upper = hl2 + MULTIPLIER * float(atr)
    basic_lower = hl2 - MULTIPLIER * float(atr)

    df.loc[i, "BASIC_UPPER"] = basic_upper
    df.loc[i, "BASIC_LOWER"] = basic_lower

    if i == ATR_PERIOD - 1 or prev_final_upper is None:
        final_upper = basic_upper
        final_lower = basic_lower
    else:
        previous_close = float(df.loc[i - 1, "close"])

        if (
            basic_upper < prev_final_upper
            or previous_close > prev_final_upper
        ):
            final_upper = basic_upper
        else:
            final_upper = prev_final_upper

        if (
            basic_lower > prev_final_lower
            or previous_close < prev_final_lower
        ):
            final_lower = basic_lower
        else:
            final_lower = prev_final_lower

    df.loc[i, "FINAL_UPPER"] = final_upper
    df.loc[i, "FINAL_LOWER"] = final_lower

    if i == ATR_PERIOD - 1 or prev_supertrend is None:
        direction = 1
        supertrend = final_upper
    else:
        if prev_supertrend == prev_final_upper:
            if close <= final_upper:
                direction = 1
                supertrend = final_upper
            else:
                direction = -1
                supertrend = final_lower
        else:
            if close >= final_lower:
                direction = -1
                supertrend = final_lower
            else:
                direction = 1
                supertrend = final_upper

    df.loc[i, "ST_DIRECTION"] = direction
    df.loc[i, "SUPERTREND"] = supertrend

    if i > 0 and prev_direction != direction:
        if direction == -1:
            df.loc[i, "SIGNAL"] = "BUY"
        elif direction == 1:
            df.loc[i, "SIGNAL"] = "SELL"

    prev_final_upper = final_upper
    prev_final_lower = final_lower
    prev_supertrend = supertrend
    prev_direction = direction

# Compatibility with the existing dashboard.
df["M_TREND"] = df["ST_DIRECTION"]


# ============================================================
# SIGNAL HISTORY
# ============================================================


# ============================================================

signal_rows = df[
    df["SIGNAL"].isin(
        ["BUY", "SELL"]
    )
].copy()

# Signal rows are retained internally only to derive the current and previous
# confirmed signal and to calculate the SuperTrend/chart accurately. No history
# table or multi-timeframe history is displayed.
signal_rows = df[df["SIGNAL"].isin(["BUY", "SELL"])].copy()

# ============================================================
# CURRENT CANDLE
# ============================================================

last_candle = df.iloc[-1]

current_trend = int(
    last_candle["M_TREND"]
)

current_close = float(
    last_candle["close"]
)

current_supertrend = float(
    last_candle["SUPERTREND"]
)

current_signal = str(
    last_candle["SIGNAL"]
)

# Discord signal alert: only for a newly observed confirmed SuperTrend flip.
if current_signal in ("BUY", "SELL"):
    _discord_signal_time = indian_time(last_candle["time"])
    _discord_signal_key = f"{SYMBOL}_{current_signal}_{_discord_signal_time}"
    # First dashboard run: baseline the already-existing signal silently. This
    # prevents delayed alerts for old confirmed signals after a restart.
    if "_discord_seen_st_signals" not in st.session_state:
        st.session_state["_discord_seen_st_signals"] = {_discord_signal_key}
        st.session_state["_discord_last_st_direction"] = current_signal
    _discord_seen_signals = st.session_state.setdefault("_discord_seen_st_signals", set())
    _discord_last_direction = str(st.session_state.get("_discord_last_st_direction", current_signal)).upper()
    # Only a real BUY<->SELL direction flip qualifies. A new candle with the
    # same continuing direction must not produce another signal-change message.
    if current_signal != _discord_last_direction and _discord_signal_key not in _discord_seen_signals:
        _discord_old_signal = "UNKNOWN"
        try:
            _signal_history = signal_rows.copy()
            _signal_history["_signal_ts"] = pd.to_datetime(_signal_history["time"], errors="coerce")
            _current_ts = pd.to_datetime(last_candle["time"], errors="coerce")
            _prior_rows = _signal_history[_signal_history["_signal_ts"] < _current_ts]
            for _, _prior_row in _prior_rows.iloc[::-1].iterrows():
                _candidate_signal = str(_prior_row.get("SIGNAL", "")).upper()
                if _candidate_signal in ("BUY", "SELL") and _candidate_signal != current_signal:
                    _discord_old_signal = _candidate_signal
                    break
        except Exception:
            pass
        _discord_actual_action = (
            f"Confirmed SuperTrend {current_signal} signal detected. Bot position status: OPEN; "
            "order placement/fill is announced separately only from Delta Exchange order records."
        )
        _discord_supertrend_signal(
            SYMBOL, current_signal, "OPEN", float(last_candle["close"]),
            OWNER_DEFAULT_QTY, _discord_signal_time,
            old_signal=_discord_old_signal, actual_action=_discord_actual_action,
        )
        _discord_seen_signals.add(_discord_signal_key)
        st.session_state["_discord_last_st_direction"] = current_signal


# ============================================================
# LIVE MARKET PRICE — ADDITIVE ONLY
# ------------------------------------------------------------
# Strategy/SuperTrend calculations continue to use completed 1H candles.
# This separate value is the live Delta ticker price used only for the
# live-price display/line on the existing chart. The normal 5-second
# Streamlit refresh keeps this price updating while the dashboard is open.
# ============================================================

live_price = current_close
try:
    _live_ticker_response = api.ticker()
    _live_ticker_result = (
        _live_ticker_response.get("result")
        if isinstance(_live_ticker_response, dict)
        else None
    )

    if isinstance(_live_ticker_result, dict):
        for _live_key in (
            "close",
            "last_price",
            "mark_price",
            "spot_price",
            "price"
        ):
            _live_value = _live_ticker_result.get(_live_key)
            if _live_value is not None and str(_live_value).strip() != "":
                live_price = float(_live_value)
                break
except Exception:
    live_price = current_close

if not isinstance(live_price, (int, float)) or live_price <= 0:
    live_price = current_close


# ============================================================
# CURRENT DIRECTION
# ============================================================

if current_trend == -1:

    current_direction = "BUY / BULLISH 🟢"

else:

    current_direction = "SELL / BEARISH 🔴"


# ============================================================
# CURRENT ENTRY
# ============================================================

if len(signal_rows) >= 1:

    current_entry = signal_rows.iloc[-1]

    signal_direction = str(
        current_entry["SIGNAL"]
    )

    signal_entry_price = float(
        current_entry["close"]
    )

    signal_supertrend = float(
        current_entry["SUPERTREND"]
    )

    signal_time = indian_time(
        current_entry["time"]
    )

else:

    signal_direction = ""

    signal_entry_price = current_close

    signal_supertrend = current_supertrend

    signal_time = indian_time(
        last_candle["time"]
    )


# ============================================================
# PREVIOUS ENTRY
# ============================================================

if len(signal_rows) >= 2:

    previous_entry = signal_rows.iloc[-2]

    previous_entry_signal = str(
        previous_entry["SIGNAL"]
    )

    previous_entry_price = float(
        previous_entry["close"]
    )

    previous_entry_st = float(
        previous_entry["SUPERTREND"]
    )

    previous_entry_time = indian_time(
        previous_entry["time"]
    )

else:

    previous_entry_signal = ""

    previous_entry_price = None

    previous_entry_st = None

    previous_entry_time = "-"


with st.expander(f"📈 SUPERTREND — {TIMEFRAME.upper()} · विवरण देखने के लिए टैप करें", expanded=False):
    st.header(f"📈 SUPERTREND — {TIMEFRAME.upper()}")

    c1, c2, c3 = st.columns(3)

    with c1:

        st.metric(
            "CURRENT DIRECTION",
            current_direction
        )

    with c2:

        st.metric(
            "CURRENT CLOSE",
            show_price(current_close)
        )

    with c3:

        st.metric(
            "SUPERTREND",
            show_price(current_supertrend)
        )


    # ============================================================
    # CURRENT ENTRY
    # ============================================================

    st.subheader("🎯 CURRENT ENTRY")

    if signal_direction == "BUY":

        st.success(
            f"🟢🚦🔉 BUY | "
            f"ENTRY: {show_price(signal_entry_price)} | "
            f"SUPERTREND: {show_price(signal_supertrend)}"
        )

    elif signal_direction == "SELL":

        st.error(
            f"🔴🚦🔉 SELL | "
            f"ENTRY: {show_price(signal_entry_price)} | "
            f"SUPERTREND: {show_price(signal_supertrend)}"
        )

    else:

        st.info(
            "No confirmed SuperTrend entry."
        )


    st.write(
        f"Signal Candle: **{signal_time}**"
    )


# ============================================================
# ADDITIVE LIVE TSL TRACKING + 1H CANDLE COUNTDOWN
# ------------------------------------------------------------
# Existing Entry / Live Price / Points display is intentionally untouched.
# TSL uses the SAME confirmed SuperTrend line already used by the strategy.
# The countdown is display-only and follows the real browser clock.
# No order, signal, grid, or existing strategy logic is changed here.
# ============================================================

_tsl_tracking_price = float(current_supertrend)
_tsl_tracking_direction = (
    "BUY / BULLISH" if current_trend == -1
    else "SELL / BEARISH"
)
_tsl_tracking_icon = "🟢" if current_trend == -1 else "🔴"
_tsl_tracking_color = "#22c55e" if current_trend == -1 else "#ff3b30"

components.html(
    f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{
    margin:0;
    padding:0;
    background:transparent;
    font-family:Arial,sans-serif;
}}
.track-wrap {{
    display:grid;
    grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);
    gap:12px;
    margin:16px 0 8px 0;
}}
.track-card {{
    min-height:142px;
    box-sizing:border-box;
    border-radius:18px;
    padding:18px 16px;
    background:linear-gradient(135deg,#0b1220,#111827);
    border:2px solid #334155;
    box-shadow:0 0 18px rgba(0,0,0,.20);
    text-align:center;
}}
.tsl-card {{
    border-color:{_tsl_tracking_color};
}}
.label {{
    font-size:clamp(16px,3.5vw,21px);
    font-weight:950;
    letter-spacing:.7px;
    color:#cbd5e1;
    margin-bottom:7px;
}}
.tsl-price {{
    font-size:clamp(34px,8vw,54px);
    line-height:1.05;
    font-weight:1000;
    color:{_tsl_tracking_color};
    text-shadow:0 0 12px rgba(255,255,255,.08);
}}
.sub {{
    margin-top:7px;
    font-size:clamp(12px,2.8vw,15px);
    font-weight:800;
    color:#94a3b8;
}}
.count {{
    font-size:clamp(38px,9vw,58px);
    line-height:1.0;
    font-weight:1000;
    color:#facc15;
    letter-spacing:1px;
    margin-top:7px;
}}
.time {{
    font-size:clamp(12px,2.8vw,15px);
    color:#93c5fd;
    font-weight:800;
    margin-top:8px;
}}
@media (max-width: 620px) {{
    .track-wrap {{
        grid-template-columns:1fr;
    }}
    .track-card {{
        min-height:128px;
    }}
}}
</style>
</head>
<body>
<div class="track-wrap">

  <div class="track-card tsl-card">
    <div class="label">{_tsl_tracking_icon} TSL TRACKING</div>
    <div class="tsl-price">{_tsl_tracking_price:,.2f}</div>
    <div class="sub">SuperTrend trailing price • {_tsl_tracking_direction}</div>
  </div>

  <div class="track-card">
    <div class="label">⏱️ CURRENT {TIMEFRAME.upper()} CANDLE</div>
    <div class="count" id="countdown">59:59</div>
    <div class="time" id="candleTime">Calculating...</div>
  </div>

</div>

<script>
function pad2(n) {{
    return String(n).padStart(2, "0");
}}

function updateCandleCountdown() {{
    // 1H candles MUST start at :30 IST (02:30, 03:30, 04:30 ...),
    // not at :00.  Calculate the boundary explicitly in IST so the
    // display remains correct even if the device/browser timezone differs.
    const nowMs = Date.now();
    const IST_OFFSET_MS = (5 * 60 + 30) * 60000;
    const HOUR_MS = 60 * 60000;
    const HALF_HOUR_MS = 30 * 60000;

    const istNowMs = nowMs + IST_OFFSET_MS;
    const candleStartIstMs =
        Math.floor((istNowMs - HALF_HOUR_MS) / HOUR_MS) * HOUR_MS
        + HALF_HOUR_MS;
    const candleEndIstMs = candleStartIstMs + HOUR_MS;

    const remaining = Math.max(0, candleEndIstMs - istNowMs);
    const totalSeconds = Math.max(0, Math.ceil(remaining / 1000));

    const mm = Math.floor(totalSeconds / 60);
    const ss = totalSeconds % 60;

    document.getElementById("countdown").textContent =
        pad2(mm) + ":" + pad2(ss);

    // Convert the IST wall-clock timestamps back to UTC Date objects.
    const start = new Date(candleStartIstMs - IST_OFFSET_MS);
    const end = new Date(candleEndIstMs - IST_OFFSET_MS);

    const fmt = {{
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
        timeZone: "Asia/Kolkata"
    }};

    document.getElementById("candleTime").textContent =
        "IST " + start.toLocaleTimeString("en-IN", fmt) +
        " → " + end.toLocaleTimeString("en-IN", fmt);
}}

updateCandleCountdown();
setInterval(updateCandleCountdown, 250);
</script>
</body>
</html>
""",
    height=305,
    scrolling=False
)


# ============================================================
with st.expander("📍 LIVE PRICE / CURRENT ENTRY — विवरण देखने के लिए टैप करें", expanded=False):
    # LIVE PRICE vs CURRENT ENTRY
    # Difference is based ONLY on the latest signal entry price.
    # Current SuperTrend is NOT used for this calculation.
    # ============================================================

    # LIVE PRICE vs CURRENT ENTRY — SIGNAL-DIRECTION COLOR FIX
    if signal_direction == "SELL":
        _live_entry_difference = signal_entry_price - live_price
    elif signal_direction == "BUY":
        _live_entry_difference = live_price - signal_entry_price
    else:
        _live_entry_difference = 0.0

    _live_entry_points = _live_entry_difference

    if _live_entry_difference > 0:
        _live_entry_color = "#16a34a"
        _live_entry_position = "FAVOURABLE TO SIGNAL"
    elif _live_entry_difference < 0:
        _live_entry_color = "#dc2626"
        _live_entry_position = "AGAINST SIGNAL"
    else:
        _live_entry_color = "#eab308"
        _live_entry_position = "AT ENTRY"

    _signal_icon = "🟢" if signal_direction == "BUY" else "🔴" if signal_direction == "SELL" else "⚪"

    st.markdown(
        f"""
        <div style="
            margin-top:18px;
            padding:18px 16px;
            border:2px solid {_live_entry_color};
            border-radius:16px;
            background:#0b1220;
            text-align:center;
        ">
            <div style="font-size:clamp(20px,5vw,28px);font-weight:1000;color:#ffffff;margin-bottom:12px;">
                {_signal_icon} CURRENT {signal_direction} SIGNAL
            </div>
            <div style="font-size:clamp(16px,4vw,21px);font-weight:900;color:#ffffff;line-height:1.7;">
                ENTRY: <span style="color:#facc15;">{signal_entry_price:,.2f}</span><br>
                LIVE PRICE: <span style="color:#38bdf8;">{live_price:,.2f}</span>
            </div>
            <div style="font-size:clamp(38px,10vw,62px);font-weight:1000;color:{_live_entry_color};line-height:1;margin:16px 0 8px;">
                {_live_entry_points:+,.2f}
            </div>
            <div style="font-size:clamp(18px,4.5vw,25px);font-weight:1000;color:{_live_entry_color};">
                POINTS {_live_entry_position}
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )


    # ============================================================
    # PREVIOUS ENTRY
    # ============================================================

    st.subheader("📜 PREVIOUS ENTRY")

    if previous_entry_signal == "BUY":

        st.success(
            f"🟢 BUY | "
            f"ENTRY: {show_price(previous_entry_price)} | "
            f"SUPERTREND: {show_price(previous_entry_st)}"
        )

    elif previous_entry_signal == "SELL":

        st.error(
            f"🔴 SELL | "
            f"ENTRY: {show_price(previous_entry_price)} | "
            f"SUPERTREND: {show_price(previous_entry_st)}"
        )

    else:

        st.info(
            "Previous entry available nahi hai."
        )


    st.write(
        f"Signal Candle: **{previous_entry_time}**"
                                              )

    # ============================================================

    # ============================================================
    # OWNER-ONLY ENGINE RUNTIME
    # ============================================================

class _SilentColumn:
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False

class _SilentStreamlit:
    def __init__(self, state):
        self.session_state = state
    def __getattr__(self, _name):
        def _noop(*_args, **_kwargs):
            if _name == "columns":
                n = _args[0] if _args else _kwargs.get("spec", 1)
                if isinstance(n, int):
                    return tuple(_SilentColumn() for _ in range(n))
                return tuple(_SilentColumn() for _ in n)
            return None
        return _noop


class _AccountStreamlit:
    """Visible Streamlit facade with account-isolated session state."""
    def __init__(self, real_streamlit, state):
        self._real = real_streamlit
        self.session_state = state

    def __getattr__(self, name):
        return getattr(self._real, name)

if "_owner_account_state" not in st.session_state:
    st.session_state["_owner_account_state"] = {}

def _account_state(account_id):
    state = st.session_state["_owner_account_state"]
    if not isinstance(state, dict):
        state = {}
        st.session_state["_owner_account_state"] = state
    return state

def _account_client(account_id):
    return DeltaAPI(OWNER_API_KEY, OWNER_API_SECRET)

def _account_credentials(account_id):
    return OWNER_API_KEY, OWNER_API_SECRET
def _run_account_engine(account_id, account_api, account_qty_btc, render_output):
    _ACCOUNT_STATE = _account_state(account_id)
    _real_st = globals()["st"]
    # Owner account are rendered, while engine/session state remains isolated.
    st = _AccountStreamlit(_real_st, _ACCOUNT_STATE)
    api = account_api
    ORDER_QTY = float(account_qty_btc)
    DEFAULT_ORDER_SIZE = _qty_to_contracts(ORDER_QTY)
    # PART 3/4 — SUPERTREND GRID ENGINE
    # ============================================================
    # User-defined grid rules:
    # 1) SuperTrend is direction selector only.
    # 2) New cycle starts from the confirmed SuperTrend signal-time ATR line.
    # 3) ALL 13 fixed levels are created immediately at signal time.
    # 4) One grid order uses the GRID QUANTITY PER ORDER setting.
    # 5) Grid spacing = 100 points.
    # 6) BUY: L1=ATR, then L2..L13 = ATR + 0.0055, +0.0110 ... +0.0660.
    # 7) SELL: L1=ATR, then L2..L13 = ATR - 0.0055, -0.0110 ... -0.0660.
    # 8) Every filled level creates its own opposite-side LIMIT target at
    #    exactly level +/- 100. Target fill creates re-entry at that SAME level.
    # 9) SELL direction is the exact mirror image.
    # 10) On SuperTrend reversal, cancel ALL bot LIMIT orders and close the
    #     old position before starting a fresh cycle.
    # 11) No attached TP orders are used. The grid itself is the target engine.
    # ============================================================

    with st.expander(f"🔴 {account_id} — FULL REAL RECORD · विवरण देखने के लिए टैप करें", expanded=False):
        st.divider()
        st.header(f"🔴 {account_id} — FULL REAL RECORD")
        st.caption(f"Owner REAL account • Quantity: {ORDER_QTY:.3f} XRP/order • Same existing SuperTrend/Grid logic")
        st.info(f"🔴 {account_id} API • REAL ACCOUNT • GRID ENGINE")
        _account_mode = OWNER_DIRECTION_MODE
        _account_effective_direction = _resolve_account_direction(_account_mode, signal_direction)
        _dir_icon = "🟢" if _account_effective_direction == "BUY" else "🔴" if _account_effective_direction == "SELL" else "⚪"
        st.success(
            f"{_dir_icon} {account_id} CURRENT TRADING DIRECTION: "
            f"{_account_effective_direction or 'WAITING FOR SIGNAL'} • CONTROL: {_account_mode}"
        )
    # XRPUSD-only grid: price-based level offsets and a 0.0040 target distance.
    # BTC point-step settings are not used in this dedicated file.
    _is_xrpusd = str(SYMBOL).upper() == "XRPUSD"
    GRID_STEP = float(os.getenv("XRPUSD_GRID_STEP", "0.0055"))
    # Every grid entry/target/re-entry follows the dashboard quantity setting.
    GRID_CHUNK_CONTRACTS = DEFAULT_ORDER_SIZE
    GRID_INITIAL_CONTRACTS = DEFAULT_ORDER_SIZE
    GRID_LEVEL_COUNT = 13

    # FINAL FIXED-GRID RULE:
    # On a NEW confirmed SuperTrend signal, all 13 fixed levels are created
    # from the signal-time ATR/SuperTrend line using XRPUSD-specific offsets.
    # The order price/level itself never changes.
    #
    # Every filled level owns its own fixed target: BUY = level + GRID_STEP,
    # SELL = level - GRID_STEP. Target is NEVER calculated from live CMP or
    # the actual fill price. After target hit, re-entry returns to the SAME
    # fixed level and the SAME fixed target cycle repeats until signal change.
    st.session_state["initial_entry_offset"] = 0

    if GRID_STEP <= 0:
        raise ValueError("GRID_STEP must be greater than 0")
    if GRID_LEVEL_COUNT != 13:
        raise ValueError("GRID_LEVEL_COUNT must remain 13")

    GRID_CHUNK_XRP = GRID_CHUNK_CONTRACTS * CONTRACT_XRP
    GRID_INITIAL_XRP = GRID_INITIAL_CONTRACTS * CONTRACT_XRP


    def _grid_result(response):
        if isinstance(response, dict):
            return response.get("result", response)
        return response


    def _grid_order_side(order):
        return str(order.get("side", "")).lower()


    def _grid_is_limit(order):
        return "limit" in str(
            order.get("order_type", order.get("type", ""))
        ).lower()


    def _grid_is_bot(order):
        cid = str(order.get("client_order_id", ""))
        return cid.startswith("GRID_")

    def _grid_has_bot_orders_for_direction(open_orders, direction):
        """RESUME DETECTION ONLY.

        Agar current direction (BUY/SELL) ke bot GRID orders Delta par
        abhi bhi open hain, to yeh True return karega. Isका use SIRF
        yeh decide karne ke liye hota hai ki dashboard restart ko safe
        RESUME maana jaye (cancel/close NAHIN) ya fresh/reversal cycle.
        """
        direction = str(direction or "").upper().strip()
        if direction == "BUY":
            prefix = "GRID_B_"
        elif direction == "SELL":
            prefix = "GRID_S_"
        else:
            return False
        for order in open_orders or []:
            if not _grid_is_bot(order):
                continue
            cid = str(order.get("client_order_id", "")).upper()
            if cid.startswith(prefix):
                return True
        return False


    def _grid_price(order):
        value = order.get("limit_price")
        try:
            return float(value)
        except Exception:
            return None


    def _grid_contracts(order):
        try:
            return int(abs(float(order.get("size", 0))))
        except Exception:
            return 0


    def _grid_order_id(order):
        value = order.get("id", order.get("order_id"))
        return str(value) if value is not None else None


    def _grid_order_fill_quantities(order):
        """Return (total contracts, filled contracts, unfilled contracts) when available."""
        def _number(keys):
            for key in keys:
                try:
                    value = order.get(key)
                    if value not in (None, ""):
                        return abs(float(value))
                except (TypeError, ValueError):
                    continue
            return None

        total = _number(("size", "order_size", "quantity", "requested_size"))
        filled = _number(("filled_size", "filled_quantity", "filled", "executed_size", "executed_quantity"))
        unfilled = _number(("unfilled_size", "remaining_size", "remaining_quantity", "leaves_qty"))
        if filled is None and total is not None and unfilled is not None:
            filled = max(0.0, total - unfilled)
        if unfilled is None and total is not None and filled is not None:
            unfilled = max(0.0, total - filled)
        return total, filled, unfilled


    def _grid_order_filled_contracts(order):
        """Filled quantity from exchange fields; never infer a fill from price alone."""
        _total, filled, _unfilled = _grid_order_fill_quantities(order)
        return float(filled or 0.0)


    def _grid_order_was_filled(order):
        """Recognize Delta fills even when its closed-order schema omits filled_size.

        Delta's closed-order response can expose average_fill_price and/or
        paid_commission without a filled_size field.  The previous implementation
        treated those valid fills as unfilled, so the target/re-entry chain never
        started.  Explicit cancellation/rejection always wins over these hints.
        """
        state = str(order.get("state", order.get("status", ""))).lower().strip()
        cancellation_reason = str(order.get("cancellation_reason", "") or "").strip().lower()
        total, filled, unfilled = _grid_order_fill_quantities(order)

        # Real executed quantity wins even if the remaining order was later
        # cancelled/rejected. A partially-filled-then-cancelled order still
        # requires a target for the executed contracts.
        if filled is not None and filled > 1e-9:
            return True
        if state in {"cancelled", "canceled", "rejected", "failed", "expired"}:
            return False

        # A fully-filled state is authoritative unless the exchange explicitly
        # provides a cancellation/rejection reason.
        if state in {"filled", "fully_filled", "fully-filled", "completed", "complete"}:
            return not any(word in cancellation_reason for word in ("cancel", "reject", "expire"))

        # Closed orders may omit filled_size but report zero remaining quantity.
        if state in {"closed", "close"}:
            if total is not None and unfilled is not None and unfilled <= 1e-9 and total > 0:
                return not any(word in cancellation_reason for word in ("cancel", "reject", "expire"))
            # Some Delta responses omit fill quantities entirely on filled orders,
            # but retain the exchange's average execution price.
            avg_fill = order.get("average_fill_price", order.get("average_filled_price"))
            try:
                if avg_fill not in (None, "") and float(avg_fill) > 0:
                    return not any(word in cancellation_reason for word in ("cancel", "reject", "expire"))
            except (TypeError, ValueError):
                pass
            # Commission > 0 is another execution hint when price/size fields are
            # missing. Zero commission alone is not treated as proof of a fill.
            try:
                commission = order.get("paid_commission", order.get("commission_paid"))
                if commission not in (None, "") and abs(float(commission)) > 1e-12:
                    return not any(word in cancellation_reason for word in ("cancel", "reject", "expire"))
            except (TypeError, ValueError):
                pass
            return False

        # Partial fills can still be active on the exchange.
        return (filled or 0) > 1e-9


    def _grid_close_position(position_size, direction, tag):
        """Close an old position with a reduce-only market order."""
        size = int(abs(float(position_size or 0)))
        if size <= 0:
            return True, None

        close_side = "sell" if float(position_size) > 0 else "buy"
        # Every new order instance gets a new client ID.  The exchange order_id
        # remains the primary identity; this client ID is only a mapping tag.
        close_cid = (
            f"GRID_CLOSE_{str(direction)[:1]}_{str(tag)[:3]}_"
            f"{uuid.uuid4().hex[:10]}"
        )[:32]
        result = api.place_market_reduce_only(
            side=close_side,
            size=size,
            client_order_id=close_cid
        )
        ok = isinstance(result, dict) and result.get("success") is True
        return ok, result


    _grid_fills_cache = {"loaded": False, "ok": False, "rows": [], "error": None}

    def _grid_fetch_fills():
        """Fetch recent executions once per dashboard engine pass."""
        if _grid_fills_cache["loaded"]:
            return _grid_fills_cache["ok"], list(_grid_fills_cache["rows"])
        _grid_fills_cache["loaded"] = True
        try:
            response = api.fills()
            ok = isinstance(response, dict) and response.get("success") is True
            rows = _grid_result(response)
            if not isinstance(rows, list):
                rows = []
            _grid_fills_cache["ok"] = ok
            _grid_fills_cache["rows"] = rows
            _grid_fills_cache["error"] = None if ok else response.get("error", response.get("message", "Delta fills request failed")) if isinstance(response, dict) else "Invalid Delta fills response"
            return ok, list(rows)
        except Exception as exc:
            _grid_fills_cache["ok"] = False
            _grid_fills_cache["rows"] = []
            _grid_fills_cache["error"] = str(exc)
            return False, []


    def _grid_attach_real_fills(order_rows):
        """Attach execution quantities to orders using Delta's immutable order_id."""
        if not isinstance(order_rows, list) or not order_rows:
            return order_rows if isinstance(order_rows, list) else []
        fills_ok, fill_rows = _grid_fetch_fills()
        if not fills_ok or not fill_rows:
            return order_rows

        totals = {}
        latest_fill = {}
        seen_fill_ids = set()
        for fill in fill_rows:
            if not isinstance(fill, dict):
                continue
            nested_order = fill.get("order") if isinstance(fill.get("order"), dict) else {}
            oid = fill.get("order_id", nested_order.get("id"))
            if oid is None:
                continue
            try:
                qty = abs(float(fill.get("size", fill.get("filled_size", fill.get("quantity", 0))) or 0))
            except (TypeError, ValueError):
                qty = 0.0
            if qty <= 0:
                continue

            # Cursor pagination should not overlap, but deduplicate defensively
            # so repeated fill rows can never inflate the executed quantity.
            fill_id = fill.get("id", fill.get("fill_id"))
            if fill_id not in (None, ""):
                identity = ("id", str(fill_id))
            else:
                identity = (
                    "fallback", str(oid), str(fill.get("created_at", fill.get("timestamp", ""))),
                    str(fill.get("price", "")), str(qty), str(fill.get("side", ""))
                )
            if identity in seen_fill_ids:
                continue
            seen_fill_ids.add(identity)

            key = str(oid)
            totals[key] = totals.get(key, 0.0) + qty
            latest_fill[key] = fill

        enriched = []
        for original in order_rows:
            if not isinstance(original, dict):
                continue
            order = dict(original)
            oid = _grid_order_id(order)
            if oid and str(oid) in totals:
                total, existing_filled, unfilled = _grid_order_fill_quantities(order)
                # Use real fill records as the source of truth. Never reduce an
                # explicitly reported larger quantity due to pagination overlap.
                filled_qty = max(float(existing_filled or 0.0), totals[str(oid)])
                order["filled_size"] = filled_qty
                if total is not None:
                    order["unfilled_size"] = max(0.0, total - filled_qty)
                order["_grid_fill_source"] = "delta_v2_fills"
                fill = latest_fill.get(str(oid), {})
                order["_grid_last_fill_price"] = fill.get("price")
                order["_grid_last_fill_created_at"] = fill.get("created_at", fill.get("updated_at"))
                # Cancelled orders can still have a genuine partial execution.
                if filled_qty > 0 and str(order.get("state", "")).lower() in {"cancelled", "canceled"}:
                    order["_grid_cancelled_after_partial_fill"] = True
            enriched.append(order)
        return enriched


    def _grid_fetch_orders():
        response = api.open_orders()
        ok = isinstance(response, dict) and response.get("success") is True
        rows = _grid_result(response)
        if not isinstance(rows, list):
            rows = []
        return ok, _grid_attach_real_fills(rows) if ok else rows


    def _grid_fetch_closed_orders():
        response = api.closed_orders()
        ok = isinstance(response, dict) and response.get("success") is True
        rows = _grid_result(response)
        if not isinstance(rows, list):
            rows = []
        return ok, _grid_attach_real_fills(rows) if ok else rows


    def _grid_fetch_position():
        response = api.position()
        ok = isinstance(response, dict) and response.get("success") is True
        data = _grid_result(response)
        if isinstance(data, list):
            pos = data[0] if data else {}
        elif isinstance(data, dict):
            pos = data
        else:
            pos = {}
        try:
            size = float(pos.get("size", 0) or 0)
        except Exception:
            size = 0.0
        return ok, pos, size


    def _grid_cancel_all_bot_limits(open_orders):
        failed = False
        cancelled = 0
        for order in list(open_orders):
            if not _grid_is_bot(order) or not _grid_is_limit(order):
                continue
            order_id = _grid_order_id(order)
            if not order_id:
                continue
            result = api.cancel_order(order_id)
            if isinstance(result, dict) and result.get("success") is True:
                cancelled += 1
            else:
                failed = True
        return not failed, cancelled


    def _grid_cancel_all_active_orders_verified(open_orders):
        """
        REVERSAL/FRESH-CYCLE HARD CLEANUP.

        Cancel EVERY active order returned by this account's Delta open-orders
        endpoint for the current product: bot orders, manual orders, ENTRY,
        TARGET, REENTRY, LIMIT, bracket/triggered orders, etc.  The exchange
        order_id is used directly; client_order_id is never required.

        A cancel response is not trusted by itself.  After each cancellation
        attempt the order is verified against Delta again.  This prevents a
        stale/ambiguous cancel response from blocking a new cycle when the
        exchange has actually already removed the order.
        """
        active_states = {
            "open", "pending", "active",
            "partially_filled", "partially-filled", "triggered"
        }
        failed = False
        cancelled = 0
        details = []

        def _is_active(order):
            state = str(order.get("state", "")).lower().strip()
            return (
                state not in {"cancelled", "canceled", "filled", "closed", "rejected", "failed"}
                and (not state or state in active_states)
            )

        # First use Delta's product-scoped account-wide cancellation endpoint.
        # This is the strongest reversal cleanup because it does not depend on
        # client_order_id, order role, or whether the order was created manually
        # or by this bot.  The `api` object is the currently processed account.
        try:
            bulk_result = api.cancel_all_open_orders_for_product()
            bulk_ok = isinstance(bulk_result, dict) and bulk_result.get("success") is True
        except Exception as exc:
            bulk_ok = False
            details.append({"bulk_cancel_error": str(exc)})

        verify_ok, verify_orders = _grid_fetch_orders()
        if verify_ok:
            pending = [o for o in verify_orders if _is_active(o)]
            if not pending:
                return True, cancelled, details
        else:
            pending = list(open_orders or [])
            failed = True

        # If Delta's bulk cancellation leaves anything active, fall back to
        # canonical single-order DELETE /v2/orders cancellation and verify each
        # exchange order id.
        if not bulk_ok:
            failed = True

        # Two passes handle an order that was added between the initial snapshot
        # and the first verification refresh.
        for _pass in range(2):
            attempted_ids = set()
            for order in list(pending):
                if not _is_active(order):
                    continue
                order_id = _grid_order_id(order)
                if not order_id or str(order_id) in attempted_ids:
                    continue
                attempted_ids.add(str(order_id))

                cid = str(order.get("client_order_id", "") or "")
                side = _grid_order_side(order)
                price = _grid_price(order)
                result = api.cancel_order(order_id)

                # Always verify against the exchange.  A failed/ambiguous POST
                # is considered successful if the order has already disappeared.
                verify_ok, verify_orders = _grid_fetch_orders()
                still_open = False
                if verify_ok:
                    for remaining in verify_orders:
                        if str(_grid_order_id(remaining)) == str(order_id) and _is_active(remaining):
                            still_open = True
                            break

                api_success = isinstance(result, dict) and result.get("success") is True
                if verify_ok and not still_open:
                    cancelled += 1
                    details.append({
                        "order_id": str(order_id),
                        "client_order_id": cid,
                        "side": side,
                        "price": price,
                        "cancel_verified": True,
                    })
                elif api_success and not verify_ok:
                    # Response explicitly confirmed cancellation, but the
                    # verification request itself failed.  Do not call this a
                    # hard success; the final refresh below will decide.
                    details.append({
                        "order_id": str(order_id),
                        "client_order_id": cid,
                        "side": side,
                        "price": price,
                        "cancel_verified": False,
                        "verify_failed": True,
                    })
                    failed = True
                else:
                    failed = True
                    details.append({
                        "order_id": str(order_id),
                        "client_order_id": cid,
                        "side": side,
                        "price": price,
                        "cancel_failed": True,
                    })

            verify_ok, verify_orders = _grid_fetch_orders()
            if not verify_ok:
                failed = True
                break
            pending = [o for o in verify_orders if _is_active(o)]
            if not pending:
                return True, cancelled, details

        # Final authoritative check.  If Delta says there are no active orders,
        # cleanup is successful even if an earlier cancel response was ambiguous.
        final_ok, final_orders = _grid_fetch_orders()
        if final_ok:
            remaining = [o for o in final_orders if _is_active(o)]
            if not remaining:
                return True, cancelled, details

        return False, cancelled, details


    def _grid_cancel_wrong_direction_orders(open_orders, current_direction, current_cycle_tag=""):
        """
        HARD EXCHANGE CLEANUP.

        Only a fully identified CURRENT-CYCLE GRID order is allowed to remain.
        Client ID is NOT required for cancellation: an order with a missing,
        malformed, unknown, stale, or wrong client ID is treated as an invalid
        transaction and cancelled by Delta's real exchange order_id.

        Orders are validated by direction + current cycle + role/level. Any active
        order that fails that validation is cancelled using Delta's exchange
        order_id. After cleanup the normal engine re-arms the correct fixed grid.
        """
        direction = str(current_direction or "").upper().strip()
        if direction not in {"BUY", "SELL"}:
            return True, 0, []

        active_states = {
            "open", "pending", "active",
            "partially_filled", "partially-filled", "triggered"
        }
        expected_prefix = "GRID_B_" if direction == "BUY" else "GRID_S_"
        cycle_tag = str(current_cycle_tag or "").strip()

        failed = False
        cancelled = 0
        cancelled_details = []

        for order in list(open_orders or []):
            state = str(order.get("state", "")).lower().strip()
            if state in {"cancelled", "canceled", "filled", "closed", "rejected", "failed"}:
                continue
            if state and state not in active_states:
                continue

            order_id = _grid_order_id(order)
            if not order_id:
                continue

            cid_raw = str(order.get("client_order_id", "")).strip()
            cid = cid_raw.upper()
            side = _grid_order_side(order)
            price = _grid_price(order)

            valid_current = False
            role = ""
            level_no = 0

            # A valid order MUST carry a current-cycle GRID identity. This also
            # deliberately rejects an order with no client ID.
            if cid.startswith(expected_prefix) and cycle_tag and f"_{cycle_tag}_" in cid:
                match = re.search(r"_(?:ENTRY|TARGET|REENTRY)?(?:_)?L(\d+)_(?:ENT|TGT|RE)_", cid)
                if match:
                    try:
                        level_no = int(match.group(1))
                    except Exception:
                        level_no = 0
                if "_ENT_" in cid:
                    role = "ENTRY"
                elif "_TGT_" in cid:
                    role = "TARGET"
                elif "_RE_" in cid:
                    role = "REENTRY"

                if role in {"ENTRY", "TARGET", "REENTRY"} and 1 <= level_no <= GRID_LEVEL_COUNT:
                    valid_current = True

            if valid_current:
                continue

            if not cid_raw:
                reason = "invalid active transaction: no Client ID; not a current-cycle GRID order"
            elif not cid.startswith("GRID_"):
                reason = "invalid active transaction: non-GRID/foreign Client ID"
            elif not cid.startswith(expected_prefix):
                reason = f"wrong GRID direction; current SuperTrend direction is {direction}"
            elif cycle_tag and f"_{cycle_tag}_" not in cid:
                reason = "stale GRID cycle; order does not belong to the current confirmed signal"
            else:
                reason = "malformed/unknown GRID transaction; no valid current level/role mapping"

            result = api.cancel_order(order_id)
            if isinstance(result, dict) and result.get("success") is True:
                cancelled += 1
                cancelled_details.append({
                    "order_id": order_id,
                    "client_order_id": cid_raw,
                    "side": side,
                    "price": price,
                    "reason": reason,
                })
            else:
                failed = True
                cancelled_details.append({
                    "order_id": order_id,
                    "client_order_id": cid_raw,
                    "side": side,
                    "price": price,
                    "reason": reason,
                    "cancel_failed": True,
                })

        return not failed, cancelled, cancelled_details


    def _grid_label_parts(label):
        """
        Internal mapping only:
          ENTRY_123456789_L8
          TARGET_123456789_L8
          REENTRY_123456789_L8

        Returns cycle-tag, level-number and role.  The actual Delta order_id
        is still the primary order identity.
        """
        match = re.search(
            r"^(ENTRY|TARGET|REENTRY)_(\d+)_L(\d+)$",
            str(label)
        )
        if not match:
            return "", 0, ""
        return match.group(2), int(match.group(3)), match.group(1)


    def _grid_client_prefix(direction, label):
        """Stable mapping prefix; the final token is unique per order instance."""
        cycle_tag, level_no, role = _grid_label_parts(label)
        if not cycle_tag or not level_no or not role:
            return f"GRID_{str(direction)[:1]}_{str(label)}_"
        role_code = {
            "ENTRY": "ENT",
            "TARGET": "TGT",
            "REENTRY": "RE",
        }.get(role, role[:3])
        return (
            f"GRID_{str(direction)[:1]}_{cycle_tag}_"
            f"L{level_no:02d}_{role_code}_"
        )


    def _grid_client_id(direction, label):
        # Delta client IDs are intentionally compact.  Cycle/level/role are
        # encoded for internal mapping, and the UUID suffix makes EVERY order
        # instance unique (including repeated TARGET/REENTRY instances).
        prefix = _grid_client_prefix(direction, label)
        return f"{prefix}{uuid.uuid4().hex[:6]}"[:32]


    def _grid_extract_order_id(response):
        """Extract Delta's real exchange order_id from a placement response."""
        if not isinstance(response, dict):
            return None

        for container in (
            response,
            response.get("result") if isinstance(response.get("result"), dict) else {},
        ):
            for key in ("id", "order_id"):
                value = container.get(key)
                if value is not None and str(value).strip():
                    return str(value)

        result = response.get("result")
        if isinstance(result, list) and result:
            first = result[0]
            if isinstance(first, dict):
                for key in ("id", "order_id"):
                    value = first.get(key)
                    if value is not None and str(value).strip():
                        return str(value)
        return None


    def _grid_record_order_identity(client_order_id, order_id, label, direction):
        """Store client_id -> Delta order_id only as an internal mapping."""
        if "grid_order_identity_map" not in st.session_state:
            st.session_state["grid_order_identity_map"] = {}

        if order_id:
            st.session_state["grid_order_identity_map"][str(client_order_id)] = {
                "order_id": str(order_id),
                "client_order_id": str(client_order_id),
                "label": str(label),
                "direction": str(direction),
            }


    def _grid_identity_for_label(label):
        """Reverse-lookup: find the recorded order identity for a cycle/level/role
        label. Only used for the fallback verification below; it never
        substitutes for Delta's own reported order state.
        """
        identity_map = st.session_state.get("grid_order_identity_map", {})
        if not isinstance(identity_map, dict):
            return None
        for _cid, _info in identity_map.items():
            if isinstance(_info, dict) and _info.get("label") == label:
                return _info
        return None


    def _grid_fetch_order_by_client_id(client_order_id):
        """Fallback-only direct order lookup via Delta's single-order endpoint.

        Bulk open/closed order pagination can occasionally lag behind an
        instantly-filled marketable LIMIT (e.g. a BUY level armed above live
        CMP that fills the same moment it is submitted). This does ONE direct
        GET for the exact client_order_id so a genuinely filled order is never
        left permanently stuck in a "not found anywhere" state.
        """
        if not client_order_id:
            return False, None
        try:
            response = api.get_order_by_client_id(client_order_id)
        except Exception:
            return False, None
        if isinstance(response, dict) and response.get("success") is True:
            result = response.get("result")
            if isinstance(result, dict):
                return True, result
        return False, None


    def _grid_order_matches_label(order, label):
        """Match an order to its cycle/level/role mapping without using price."""
        cid = str(order.get("client_order_id", "")).strip()
        if not cid or not _grid_is_bot(order):
            return False

        cycle_tag, level_no, role = _grid_label_parts(label)
        if not cycle_tag or not level_no or not role:
            return False

        role_code = {
            "ENTRY": "ENT",
            "TARGET": "TGT",
            "REENTRY": "RE",
        }.get(role, role[:3])

        return (
            f"_{cycle_tag}_L{level_no:02d}_{role_code}_"
            in cid
        )


    def _grid_verify_client_instance(client_order_id):
        """
        If an API response is uncertain, verify the exact client ID against
        Delta open and closed orders before any retry is allowed.
        """
        open_ok, open_orders = _grid_fetch_orders()
        if open_ok:
            for order in open_orders:
                if str(order.get("client_order_id", "")) == str(client_order_id):
                    return True, order

        closed_ok, closed_orders = _grid_fetch_closed_orders()
        if closed_ok:
            for order in closed_orders:
                if str(order.get("client_order_id", "")) == str(client_order_id):
                    return True, order

        if not open_ok or not closed_ok:
            return None, None
        return False, None


    def _grid_place_limit(
        side, price, label, direction, size=GRID_CHUNK_CONTRACTS, reduce_only=False
    ):
        """
        Submit one LIMIT order with a unique client_order_id.

        Duplicate protection:
          1. Check Delta open orders for the same cycle/level/role intent.
          2. Submit exactly one new client ID.
          3. If the API response is uncertain, query Delta by that exact client
             ID before permitting any later retry.
        """
        open_ok, open_orders = _grid_fetch_orders()
        if not open_ok:
            return {
                "success": False,
                "error": "Delta open-order verification failed before LIMIT submit."
            }

        for existing in open_orders:
            if (
                _grid_is_bot(existing)
                and _grid_is_limit(existing)
                and _grid_order_matches_label(existing, label)
            ):
                existing_id = _grid_order_id(existing)
                existing_cid = str(existing.get("client_order_id", ""))
                existing_price = _grid_price(existing)
                existing_size = _grid_contracts(existing)
                existing_side = _grid_order_side(existing)
                existing_reduce_only_raw = existing.get("reduce_only", False)
                existing_reduce_only = (
                    existing_reduce_only_raw is True
                    or str(existing_reduce_only_raw).strip().lower() in {"true", "1", "yes"}
                )
                price_matches = (
                    existing_price is not None
                    and abs(float(existing_price) - float(price)) <= 0.01
                )
                exact_match = (
                    bool(existing_id)
                    and existing_side == str(side).lower()
                    and price_matches
                    and existing_size == int(size)
                    and existing_reduce_only == bool(reduce_only)
                )
                if not exact_match:
                    return {
                        "success": False,
                        "error": (
                            f"Existing {label} order does not match requested side/price/size/reduce_only; "
                            f"not treating it as armed and not submitting a duplicate. "
                            f"Existing id={existing_id}, side={existing_side}, price={existing_price}, "
                            f"size={existing_size}, reduce_only={existing_reduce_only}; requested "
                            f"side={str(side).lower()}, price={price}, size={int(size)}, "
                            f"reduce_only={bool(reduce_only)}."
                        ),
                    }
                _grid_record_order_identity(
                    existing_cid, existing_id, label, direction
                )
                return {
                    "success": True,
                    "result": existing,
                    "id": existing_id,
                    "order_id": existing_id,
                    "client_order_id": existing_cid,
                    "verified_existing": True,
                }

        cid = _grid_client_id(direction, label)

        result = api.place_limit_order(
            side=side,
            size=int(size),
            limit_price=float(price),
            take_profit_price=None,
            client_order_id=cid,
            reduce_only=reduce_only
        )

        order_id = _grid_extract_order_id(result)
        # ARMED is ONLY a real Delta acceptance with a valid exchange Order ID.
        if isinstance(result, dict) and result.get("success") is True and order_id:
            _grid_record_order_identity(cid, order_id, label, direction)
            result.setdefault("client_order_id", cid)
            result.setdefault("order_id", order_id)
            return result

        # The POST may have reached Delta even if the HTTP/API response was
        # timeout/uncertain.  NEVER blindly submit another order.
        verified, existing = _grid_verify_client_instance(cid)
        if verified is True:
            existing_id = _grid_order_id(existing)
            if not existing_id:
                return {
                    "success": False,
                    "error": (
                        "Delta returned a matching client_order_id but no exchange order ID; "
                        "the order is not marked armed. Verify the order in Delta before retrying."
                    ),
                    "client_order_id": cid,
                }
            _grid_record_order_identity(cid, existing_id, label, direction)
            return {
                "success": True,
                "result": existing,
                "id": existing_id,
                "order_id": existing_id,
                "client_order_id": cid,
                "verified_after_uncertain_response": True,
            }

        if verified is None:
            return {
                "success": False,
                "error": (
                    "LIMIT response was uncertain and Delta verification "
                    "also failed; no retry was submitted."
                ),
                "client_order_id": cid,
            }

        return result


    def _grid_place_market(side, label, direction, size=GRID_CHUNK_CONTRACTS):
        cid = _grid_client_id(direction, label)
        return api.place_market_order(
            side=side,
            size=int(size),
            client_order_id=cid
        )


    def _grid_has_price_side(open_orders, side, price, tolerance=0.01):
        for order in open_orders:
            if not _grid_is_bot(order):
                continue
            if _grid_order_side(order) != side:
                continue
            op = _grid_price(order)
            if op is not None and abs(op - float(price)) <= tolerance:
                return True
        return False


    def _grid_has_client_label(open_orders, label):
        """
        Return True ONLY while the exact GRID order is still active on Delta.

        Important lifecycle rule:
          FILLED/CLOSED entry must no longer be treated as a pending entry.
          Once Delta reports it filled, the green pending line disappears and
          the target lifecycle is allowed to start.
        """
        active_states = {"open", "pending", "active", "partially_filled", "partially-filled"}
        for order in open_orders:
            if not _grid_is_bot(order):
                continue
            state = str(order.get("state", "")).lower().strip()
            if state and state not in active_states:
                continue
            if _grid_order_matches_label(order, label):
                order_id = _grid_order_id(order)
                cid = str(order.get("client_order_id", ""))
                if order_id and cid:
                    _grid_record_order_identity(
                        cid, order_id, label, ""
                    )
                return True
        return False


    def _grid_client_role_level(order):
        """Return (role, level_no) from a GRID client_order_id.

        This works for LIMIT and MARKET orders because the lifecycle identity is
        carried by client_order_id, while the Delta exchange order_id remains the
        actual order identity.
        """
        cid = str(order.get("client_order_id", "")).strip()
        if not cid.startswith("GRID_"):
            return "", 0
        match = re.search(r"_L(\d+)_([A-Z]+)_", cid.upper())
        if not match:
            return "", 0
        level_no = int(match.group(1))
        role_code = match.group(2)
        role = {"ENT": "ENTRY", "TGT": "TARGET", "RE": "REENTRY"}.get(role_code, "")
        return role, level_no


    def _grid_order_event_epoch(order):
        """Best available exchange timestamp for ordering lifecycle events."""
        for key in (
            "updated_at", "filled_at", "executed_at", "closed_at",
            "created_at_ts", "created_at", "created_time", "timestamp"
        ):
            value = order.get(key)
            if value in (None, ""):
                continue
            try:
                ts = float(value)
                if ts > 10_000_000_000:
                    ts /= 1000.0
                return ts
            except Exception:
                continue
        try:
            return float(order.get("id", 0) or 0)
        except Exception:
            return 0.0


    def _grid_active_bot_orders(open_orders):
        return [
            o for o in open_orders
            if _grid_is_bot(o)
            and _grid_is_limit(o)
            and str(o.get("state", "")).lower() not in {"cancelled", "filled", "rejected"}
        ]


    def _grid_signal_key(direction, price, signal_time):
        try:
            epoch = int(float(signal_time))
        except Exception:
            epoch = int(time.time())
        return f"{direction}_{epoch}_{int(round(float(price)))}"


    # ------------------------------------------------------------
    # GRID ACCOUNT / ORDER SNAPSHOT
    # ------------------------------------------------------------
    grid_open_ok, grid_open_orders = _grid_fetch_orders()
    grid_pos_ok, grid_position, grid_position_size = _grid_fetch_position()

    # ------------------------------------------------------------
    # CLEAN-ONCE STARTUP GATE
    # Every fresh dashboard session clears old product orders and closes an
    # old product position before the strategy can place a new grid. This is
    # deliberately done once per session, not on every 5-second rerun.
    # WARNING: in REAL mode this will cancel XRPUSD orders and close XRPUSD
    # position on dashboard startup, as explicitly requested by the owner.
    # ------------------------------------------------------------
    _startup_cleanup_ok = bool(st.session_state.get("startup_cleanup_done", False))
    if not _startup_cleanup_ok:
        if grid_open_ok and _grid_has_bot_orders_for_direction(grid_open_orders, _account_effective_direction):
            st.success(
                f"🔄 RESUME DETECTED AT STARTUP: existing {_account_effective_direction} GRID orders already "
                "found on Delta. Skipping full startup cleanup — no cancel, no position close."
            )
            st.session_state["startup_cleanup_done"] = True
            _startup_cleanup_ok = True
        elif not grid_open_ok or not grid_pos_ok:
            st.error("🛑 STARTUP CLEANUP BLOCKED: Delta open orders/position could not be verified. No new grid actions are allowed.")
            _startup_cleanup_ok = False
        else:
            _startup_cancel_ok, _startup_cancelled, _startup_cancel_details = _grid_cancel_all_active_orders_verified(grid_open_orders)
            _startup_cleanup_ok = bool(_startup_cancel_ok)
            if not _startup_cancel_ok:
                st.error("🛑 STARTUP CLEANUP BLOCKED: one or more old orders could not be confirmed cancelled. No new grid actions are allowed.")
            if _startup_cleanup_ok and abs(grid_position_size) >= 0.000001:
                _startup_close_ok, _startup_close_result = _grid_close_position(
                    grid_position_size, "", "STARTUP"
                )
                if not _startup_close_ok:
                    st.error(f"🛑 STARTUP CLEANUP BLOCKED: old position close request failed: {_startup_close_result}")
                    _startup_cleanup_ok = False
                else:
                    _startup_cleanup_ok = False
                    for _startup_attempt in range(6):
                        time.sleep(0.5)
                        _startup_pos_ok, _startup_position, _startup_position_size = _grid_fetch_position()
                        if _startup_pos_ok and abs(_startup_position_size) < 0.000001:
                            _startup_cleanup_ok = True
                            break
                    if not _startup_cleanup_ok:
                        st.error("🛑 STARTUP CLEANUP BLOCKED: Delta has not confirmed the old position is closed. No new grid actions are allowed.")
            if _startup_cleanup_ok:
                _startup_orders_ok, _startup_orders_after = _grid_fetch_orders()
                _startup_pos_ok, _startup_position_after, _startup_size_after = _grid_fetch_position()
                _startup_cleanup_ok = bool(
                    _startup_orders_ok and _startup_pos_ok
                    and not _startup_orders_after
                    and abs(_startup_size_after) < 0.000001
                )
                if _startup_cleanup_ok:
                    st.session_state["startup_cleanup_done"] = True
                    st.success("🧹 Startup verified: no old XRPUSD orders and no open XRPUSD position. Grid engine may now start.")
                else:
                    st.error("🛑 STARTUP CLEANUP BLOCKED: final Delta verification still finds orders/position or the API check failed.")

    if not grid_open_ok:
        st.error("🛑 Grid engine stopped: Delta open-order check failed.")
    elif not grid_pos_ok:
        st.error("🛑 Grid engine stopped: Delta position check failed.")


    # ------------------------------------------------------------
    # ACCOUNT API CONNECTION STATUS ONLY — balance/margin panels removed.
    # ------------------------------------------------------------
    _qty_error = "Live wallet balance unavailable; new grid orders blocked."
    _qty_signal_change_pending = False
    try:
        # production ACCOUNT CONNECTION CHECK — credentials alone never count as connected.
        wallet_response = api.wallet_balances()
        leverage_response = api.order_leverage()
        # Update quantity ONLY when the confirmed SuperTrend signal direction
        # changes. Balance movement during the same signal cannot change it.
        _qty_state = _load_quantity_signal_state()
        _raw_signal = str(signal_direction or "").upper().strip()
        _qty_error = ""
        _available_delta_usd = None
        _applied_qty = _qty_state.get("applied_quantity")
        _stored_signal = str(_qty_state.get("signal_direction", "")).upper().strip()
        _qty_signal_change_pending = False
        try:
            _available_delta_usd = _available_delta_usd_from_wallet(wallet_response)
            if _raw_signal not in {"BUY", "SELL"}:
                _qty_error = "Waiting for a valid confirmed BUY/SELL SuperTrend signal."
            elif not _stored_signal:
                # First signal: do not commit a quantity until the engine has
                # verified cleanup (no active orders and no open position).
                _qty_signal_change_pending = True
            elif _raw_signal != _stored_signal:
                # IMPORTANT: defer recalculation until after reversal cleanup,
                # position closure and exchange verification. Balance is read
                # again only after cleanup succeeds.
                _qty_signal_change_pending = True
            elif _applied_qty is None:
                _qty_error = "Persisted quantity is missing; refusing to infer a replacement on the same signal."
        except Exception as _qty_exc:
            _qty_error = str(_qty_exc)
        if _applied_qty is not None:
            try:
                _applied_qty = int(_applied_qty)
                if _applied_qty < 1:
                    _qty_error = "Calculated quantity is below 1 XRP; new orders must remain blocked."
                else:
                    ORDER_QTY = float(_applied_qty)
                    DEFAULT_ORDER_SIZE = _qty_to_contracts(ORDER_QTY)
                    GRID_CHUNK_CONTRACTS = DEFAULT_ORDER_SIZE
                    GRID_INITIAL_CONTRACTS = DEFAULT_ORDER_SIZE
                    GRID_CHUNK_XRP = GRID_CHUNK_CONTRACTS * CONTRACT_XRP
                    GRID_INITIAL_XRP = GRID_INITIAL_CONTRACTS * CONTRACT_XRP
            except Exception as _qty_exc:
                _qty_error = f"Quantity validation failed: {_qty_exc}"
        wallet_data = _grid_result(wallet_response)
        leverage_data = _grid_result(leverage_response)
        _api_ok = (isinstance(wallet_response, dict) and wallet_response.get("success") is True)
        _lev_ok = (isinstance(leverage_response, dict) and leverage_response.get("success") is True)
        _api_connected = bool(_api_ok and _lev_ok)
        try:
            _public_ip = requests.get("https://api.ipify.org?format=json", timeout=3).json().get("ip", "-")
        except Exception:
            _public_ip = "Unavailable"
        _api_status_color = "#22c55e" if _api_connected else "#ef4444"
        _api_status_text = "CONNECTED & LIVE" if _api_connected else "DISCONNECTED / API ERROR"
        st.markdown(
            f'<div style="padding:14px;border:2px solid {_api_status_color};border-radius:14px;margin:8px 0 14px 0;">'
            f'<div style="font-size:1.15rem;font-weight:900;color:{_api_status_color};">● {account_id} API — {_api_status_text}</div>'
            f'<div style="margin-top:6px;color:#cbd5e1;font-weight:700;">Server Public IP: <span style="color:#f8fafc;">{_public_ip}</span></div>'
            f'<div style="margin-top:3px;color:#94a3b8;font-size:.86rem;">API keys/secrets are never displayed.</div>'
            f'</div>', unsafe_allow_html=True
        )

        # Balance, margin, leverage, daily PNL/history and account-position panels are intentionally hidden.

    except Exception as exc:
        _qty_error = f"Account API / live quantity validation failed: {exc}"
        st.warning(f"Account API status unavailable: {exc}")


    # ------------------------------------------------------------
    # GRID LEVEL CALCULATION — 16 FIXED LEVELS / ATR ANCHOR
    # ------------------------------------------------------------
    # FINAL USER LOGIC:
    #   • On every confirmed SuperTrend BUY/SELL flip, the SuperTrend ATR
    #     line is the immutable Level #1 Anchor.
    #   • BUY:  L1 = ATR, then L2..L13 = ATR + 0.0055, +0.0110 ... +0.0660.
    #   • SELL: L1 = ATR, then L2..L13 = ATR - 0.0055, -0.0110 ... -0.0660.
    #   • The 25 prices never move during the cycle.
    #   • On bot startup / fresh cycle, ALL 13 fixed levels are armed.
    #     Crossed BUY levels (<= CMP) or crossed SELL levels (>= CMP) are sent
    #     as LIMIT orders at their exact fixed prices.
    #   • The actual fill price never changes the level's target.
    #     Every target is calculated only from that level's fixed price.
    #   • After target hit, re-entry returns to that SAME fixed level and its
    #     SAME fixed target repeats.
    # ------------------------------------------------------------

    # Resolve this account's direction independently from the global SuperTrend.
    # SUPER TREND = same direction; OPPOSITE = reverse direction; BUY/SELL = manual lock.
    _direction_mode = OWNER_DIRECTION_MODE
    grid_direction = _resolve_account_direction(_direction_mode, signal_direction)
    grid_signal_base = float(signal_entry_price)
    grid_atr_line = float(signal_supertrend)

    # Stable identity for the currently confirmed SuperTrend signal cycle.
    # It must be defined before the fresh-cycle/reversal logic below.
    try:
        _signal_epoch_for_level = float(current_entry["time"])
        if _signal_epoch_for_level > 10_000_000_000:
            _signal_epoch_for_level /= 1000.0
    except Exception:
        try:
            _signal_epoch_for_level = float(pd.Timestamp(signal_time).timestamp())
        except Exception:
            _signal_epoch_for_level = 0.0

    new_cycle_key = (f"{TIMEFRAME}|ATR{ATR_PERIOD}|M{MULTIPLIER:.4f}|{grid_direction}|{int(_signal_epoch_for_level)}|{grid_atr_line:.8f}" if grid_direction else "")

    # ------------------------------------------------------------
    # TICK SIZE ROUNDING HELPER
    # XRPUSD uses 0.0001 tick size. Keep other symbols' existing grid
    # spacing and use their configured/known tick precision where applicable.
    # ------------------------------------------------------------
    XRPUSD_TICK_SIZE = 0.0001

    def _round_to_tick(price, tick_size=None):
        """Round a price to a valid exchange tick before order submission."""
        if tick_size is None:
            tick_size = XRPUSD_TICK_SIZE if str(SYMBOL).upper() == "XRPUSD" else 0.1
        tick_size = float(tick_size)
        if tick_size <= 0:
            raise ValueError("tick_size must be greater than 0")
        decimals = max(0, len(f"{tick_size:.12f}".rstrip("0").split(".")[-1]))
        return round(round(float(price) / tick_size) * tick_size, decimals)

    def _build_fixed_13_level_map(atr_line, direction):
        """Immutable 13-level map. Level #1 is ALWAYS the ATR anchor.

        Live price is not used to calculate levels; it is only used later by
        the existing order flow to determine whether a fixed level is crossed.
        """
        if str(SYMBOL).upper() == "XRPUSD":
            xrp_level_offsets = (
                0.0000, 0.0055, 0.0110, 0.0165, 0.0220,
                0.0275, 0.0330, 0.0385, 0.0440, 0.0495,
                0.0550, 0.0605, 0.0660,
            )
            if direction == "BUY":
                levels = [_round_to_tick(float(atr_line) + offset) for offset in xrp_level_offsets]
            elif direction == "SELL":
                levels = [_round_to_tick(float(atr_line) - offset) for offset in xrp_level_offsets]
            else:
                return []
        elif direction == "BUY":
            levels = [
                _round_to_tick(float(atr_line) + GRID_STEP * i)
                for i in range(GRID_LEVEL_COUNT)
            ]
        elif direction == "SELL":
            levels = [
                _round_to_tick(float(atr_line) - GRID_STEP * i)
                for i in range(GRID_LEVEL_COUNT)
            ]
        else:
            return []

        return levels


    # Owner account uses the fixed 13-level grid calculation.
    grid_levels = (
        _build_fixed_13_level_map(grid_atr_line, grid_direction)
        if grid_direction
        else []
    )

    # IMMUTABLE ORIGINAL LEVEL MAP
    # ------------------------------------------------------------
    # Every level keeps the exact price assigned when this SuperTrend
    # signal-cycle was created. Target calculations MUST use this map.
    # They must NEVER use actual exchange fill price, average fill price,
    # live/CMP price, or any later market price.
    #
    # Example: if L5/L6/L7/L8 are all filled by the exchange at 85,300
    # because price jumped through several limits, their targets still
    # come from their ORIGINAL L5/L6/L7/L8 prices respectively.
    fixed_level_price_map = {
        int(level_no): float(level_price)
        for level_no, level_price in enumerate(grid_levels, start=1)
    }

    def _original_level_price(level_no):
        """Return the immutable signal-time price for this level."""
        level_no = int(level_no)
        if level_no not in fixed_level_price_map:
            return None
        return float(fixed_level_price_map[level_no])

    def _target_from_original_level(level_no, direction):
        """Build a tick-aligned target ONLY from the original fixed grid level."""
        original_price = _original_level_price(level_no)
        if original_price is None:
            return None

        if direction == "BUY":
            return _round_to_tick(original_price + GRID_STEP)
        if direction == "SELL":
            return _round_to_tick(original_price - GRID_STEP)
        return None

    # There is NO special/nearest "current level" anymore.
    # All 16 levels are real levels and all 25 participate in the first
    # startup cycle.
    current_level_no = 0
    current_level_price = float(grid_atr_line)

    pending_entry_levels = [
        (i, float(price))
        for i, price in enumerate(grid_levels, start=1)
    ]

    outer_levels = [
        (i, float(price))
        for i, price in enumerate(grid_levels, start=1)
    ]

    upper_levels = [price for _, price in outer_levels]
    lower_levels = [price for _, price in outer_levels]


    # ------------------------------------------------------------
    # DISPLAY GRID PLAN
    # ------------------------------------------------------------
    with st.expander("🧱 ACTIVE GRID PLAN — विवरण देखने के लिए टैप करें", expanded=False):
        st.subheader("🧱 ACTIVE GRID PLAN")

        if grid_direction:
            g1, g2, g3, g4 = st.columns(4)
            with g1:
                st.metric("DIRECTION", grid_direction)
            with g2:
                st.metric("LEVELS", f"{GRID_LEVEL_COUNT}")
            with g3:
                st.metric("STEP", f"{GRID_STEP} POINTS")
            with g4:
                st.metric("ATR LINE / L1", show_price(grid_atr_line))

            st.write(
                f"STARTUP: **ALL {GRID_LEVEL_COUNT} FIXED LEVELS × "
                f"{GRID_CHUNK_XRP:.3f} XRP** एक साथ arm होंगे."
            )
            st.write(
                "Startup पर सभी fixed levels **LIMIT** ही रहते हैं. "
                "Marketable crossed LIMIT exchange पर उसी fixed price पर "
                "तुरंत fill हो सकता है; किसी level को MARKET में convert नहीं किया जाता."
            )
            st.write(
                "हर filled level का target उसी level से fixed "
                f"**{GRID_STEP:.4f} USDT** दूर रहेगा. Target hit होने पर उसी level पर "
                "fixed-price re-entry और फिर वही fixed target दोबारा बनेगा."
            )
        else:
            st.info("Confirmed SuperTrend BUY/SELL signal ka wait hai.")


    # ------------------------------------------------------------
    # DIRECTION CHANGE / FRESH-CYCLE CONTROL
    # ------------------------------------------------------------
    if "grid_last_direction" not in st.session_state:
        st.session_state["grid_last_direction"] = ""
    if "grid_cycle_key" not in st.session_state:
        st.session_state["grid_cycle_key"] = ""

    previous_grid_direction = st.session_state.get("grid_last_direction", "")
    grid_cycle_ready = (
        bool(grid_direction)
        and bool(new_cycle_key)
        and new_cycle_key == st.session_state.get("grid_cycle_key", "")
        and grid_open_ok
        and grid_pos_ok
    )
    direction_changed = (
        bool(previous_grid_direction)
        and bool(grid_direction)
        and previous_grid_direction != grid_direction
    )

    fresh_signal_cycle = (
        bool(new_cycle_key)
        and new_cycle_key != st.session_state.get("grid_cycle_key", "")
    )


    # ------------------------------------------------------------
    # SAFETY: CANCEL ANY ACTIVE ORDER FROM THE WRONG DIRECTION
    # ------------------------------------------------------------
    # This check runs on EVERY refresh.  It is deliberately broader than the
    # normal reversal cleanup: stale opposite-direction GRID orders are removed,
    # and any non-GRID/manual active order on the opposite side is removed too.
    # Current-cycle GRID TARGET orders are preserved because their opposite side
    # is intentional and is part of the valid target lifecycle.
    wrong_direction_cleanup_ok = True
    wrong_direction_cancelled = 0
    wrong_direction_cancelled_details = []

    if grid_direction and grid_open_ok:
        try:
            _cleanup_cycle_tag = str(int(float(_signal_epoch_for_level)))[-9:]
        except Exception:
            _cleanup_cycle_tag = ""
        (
            wrong_direction_cleanup_ok,
            wrong_direction_cancelled,
            wrong_direction_cancelled_details,
        ) = _grid_cancel_wrong_direction_orders(
            grid_open_orders,
            grid_direction,
            _cleanup_cycle_tag,
        )

        if wrong_direction_cancelled_details:
            failed_items = [
                item for item in wrong_direction_cancelled_details
                if item.get("cancel_failed")
            ]
            if failed_items:
                st.error(
                    f"🛑 WRONG-DIRECTION SAFETY: {len(failed_items)} order(s) "
                    "could not be cancelled. New grid actions remain blocked "
                    "until Delta confirms cleanup."
                )
            else:
                st.warning(
                    f"🛑 WRONG-DIRECTION SAFETY: "
                    f"{wrong_direction_cancelled} conflicting active order(s) "
                    f"cancelled immediately. Current direction: {grid_direction}."
                )

            # Never continue with a stale pre-cancel snapshot.
            grid_open_ok, grid_open_orders = _grid_fetch_orders()

        if not wrong_direction_cleanup_ok:
            st.error(
                "🛑 Wrong-direction order cleanup failed. "
                "Grid placement is blocked for this refresh."
            )


    # ------------------------------------------------------------
    # REVERSAL / FRESH CYCLE: CANCEL ALL ACTIVE ORDERS + CLOSE POSITION
    # ------------------------------------------------------------
    # IMPORTANT: this block intentionally does NOT depend on the earlier
    # wrong-direction cleanup result.  If that earlier cleanup failed or returned
    # an ambiguous response, this account-wide verified cleanup gets one direct
    # chance to reconcile the real Delta order state before the new grid is armed.
    _grid_resume_detected = (
        bool(grid_direction)
        and not direction_changed
        and grid_open_ok
        and _grid_has_bot_orders_for_direction(grid_open_orders, grid_direction)
    )

    if _grid_resume_detected:
        st.success(
            f"🔄 RESUME DETECTED: existing {grid_direction} GRID orders already found on Delta "
            "for this same direction. No cancel, no close — missing/left-over levels will be "
            "re-checked and re-armed by the normal per-level engine below."
        )
        st.session_state["grid_cycle_key"] = new_cycle_key
        st.session_state["grid_current_level_no"] = int(current_level_no)
        st.session_state["grid_last_direction"] = grid_direction
        grid_cycle_ready = True

    elif grid_direction and (direction_changed or fresh_signal_cycle) and _startup_cleanup_ok:
        if grid_open_ok and grid_pos_ok:
            cancel_ok, cancelled_count, cancelled_details = _grid_cancel_all_active_orders_verified(
                grid_open_orders
            )

            if not cancel_ok:
                st.error(
                    "🛑 New grid blocked: Delta still reports one or more active "
                    "old/manual/bot orders after account-wide cleanup. "
                    "No new grid was placed."
                )
            else:
                if previous_grid_direction:
                    close_ok, close_result = _grid_close_position(
                        grid_position_size,
                        grid_direction,
                        "REVERSAL"
                    )
                    if not close_ok:
                        st.error("🛑 New grid blocked: old position could not be closed.")
                    else:
                        time.sleep(0.5)
                        verify_pos_ok, verify_position, verify_position_size = _grid_fetch_position()
                        if not verify_pos_ok:
                            st.error("🛑 New grid blocked: old position closure could not be verified with Delta.")
                        elif abs(verify_position_size) >= 0.000001:
                            st.error(
                                f"🛑 New grid blocked: old position is still open ({verify_position_size} contracts)."
                            )
                        else:
                            st.warning(
                                f"🔄 Direction changed {previous_grid_direction} → {grid_direction}. "
                                f"{cancelled_count} active order(s) cancelled (bot + manual), old position closed."
                            )
                            grid_open_ok, grid_open_orders = _grid_fetch_orders()
                            grid_pos_ok, grid_position, grid_position_size = _grid_fetch_position()
                            st.session_state["grid_cycle_key"] = new_cycle_key
                            st.session_state["grid_current_level_no"] = int(current_level_no)
                            st.session_state["grid_last_direction"] = grid_direction
                            grid_cycle_ready = True
                else:
                    if previous_grid_direction:
                        close_ok, close_result = _grid_close_position(
                            grid_position_size,
                            grid_direction,
                            "REVERSAL"
                        )
                        if not close_ok:
                            st.error("🛑 New grid blocked: old position could not be closed.")
                        else:
                            time.sleep(0.5)
                            verify_pos_ok, verify_position, verify_position_size = _grid_fetch_position()
                            if not verify_pos_ok:
                                st.error("🛑 New grid blocked: old position closure could not be verified with Delta.")
                            elif abs(verify_position_size) >= 0.000001:
                                st.error(
                                    f"🛑 New grid blocked: old position is still open ({verify_position_size} contracts)."
                                )
                            else:
                                st.warning(
                                    f"🔄 Direction changed {previous_grid_direction} → {grid_direction}. "
                                    f"{cancelled_count} old GRID LIMIT order(s) cancelled and old position closed."
                                )
                                grid_open_ok, grid_open_orders = _grid_fetch_orders()
                                grid_pos_ok, grid_position, grid_position_size = _grid_fetch_position()
                                st.session_state["grid_cycle_key"] = new_cycle_key
                                st.session_state["grid_current_level_no"] = int(current_level_no)
                                st.session_state["grid_last_direction"] = grid_direction
                                grid_cycle_ready = True
                    else:
                        # FIRST START / DASHBOARD REOPEN: an empty Streamlit
                        # session has no saved direction, but exchange orders or
                        # a position may still exist. Cancel all product orders,
                        # close any old position, and verify both before arming.
                        verify_open_ok, verify_open_orders = _grid_fetch_orders()
                        verify_pos_ok, verify_position, verify_position_size = _grid_fetch_position()
                        if not verify_open_ok or not verify_pos_ok:
                            st.error("🛑 First-cycle cleanup failed: Delta orders/position could not be verified. No new grid was placed.")
                        else:
                            startup_close_ok = True
                            if abs(verify_position_size) >= 0.000001:
                                startup_close_ok, startup_close_result = _grid_close_position(
                                    verify_position_size, grid_direction, "STARTUP"
                                )
                                if startup_close_ok:
                                    for _verify_attempt in range(4):
                                        time.sleep(0.5)
                                        verify_pos_ok, verify_position, verify_position_size = _grid_fetch_position()
                                        if verify_pos_ok and abs(verify_position_size) < 0.000001:
                                            break
                                if not startup_close_ok:
                                    st.error(f"🛑 First-cycle cleanup failed: existing position close request was rejected: {startup_close_result}")
                            if startup_close_ok:
                                verify_open_ok, verify_open_orders = _grid_fetch_orders()
                                verify_pos_ok, verify_position, verify_position_size = _grid_fetch_position()
                                if (verify_open_ok and verify_pos_ok
                                        and not verify_open_orders
                                        and abs(verify_position_size) < 0.000001):
                                    st.warning("🧹 Startup cleanup verified: old orders cleared and old position closed. Starting the new signal cycle.")
                                    st.session_state["grid_cycle_key"] = new_cycle_key
                                    st.session_state["grid_current_level_no"] = int(current_level_no)
                                    st.session_state["grid_last_direction"] = grid_direction
                                    grid_cycle_ready = True
                                else:
                                    st.error("🛑 New grid blocked: first-cycle cleanup verification failed. Old orders/position must be absent before new entries.")
        else:
            st.error("🛑 Fresh grid blocked because Delta order/position verification failed.")


    # ------------------------------------------------------------
    # RE-FETCH AFTER ANY CLEANUP
    # ------------------------------------------------------------
    grid_open_ok, grid_open_orders = _grid_fetch_orders()
    grid_pos_ok, grid_position, grid_position_size = _grid_fetch_position()

    # QUANTITY COMMIT POINT: only after confirmed signal-cycle cleanup.
    # This guarantees the new balance is sampled after old orders are cancelled
    # and the old position is verified flat. A failed cleanup leaves the old
    # persisted signal/quantity unchanged and blocks a new grid.
    if _qty_signal_change_pending and grid_cycle_ready and grid_open_ok and grid_pos_ok:
        try:
            _final_open_orders_ok = isinstance(grid_open_orders, list) and len(grid_open_orders) == 0
            _final_position_flat = abs(float(grid_position_size or 0)) < 0.000001
            if not _final_open_orders_ok or not _final_position_flat:
                _qty_error = "Signal changed, but old orders/position are not fully cleared; quantity update and new orders blocked."
                grid_cycle_ready = False
                st.error(_qty_error)
            else:
                _post_cleanup_wallet = api.wallet_balances()
                _post_cleanup_balance = _available_delta_usd_from_wallet(_post_cleanup_wallet)
                _new_qty = _quantity_from_available_usd(_post_cleanup_balance)
                _new_state = {
                    "signal_direction": str(signal_direction or "").upper().strip(),
                    "applied_quantity": int(_new_qty),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                _save_quantity_signal_state(_new_state)
                ORDER_QTY = float(_new_qty)
                DEFAULT_ORDER_SIZE = _qty_to_contracts(ORDER_QTY)
                GRID_CHUNK_CONTRACTS = DEFAULT_ORDER_SIZE
                GRID_INITIAL_CONTRACTS = DEFAULT_ORDER_SIZE
                GRID_CHUNK_XRP = GRID_CHUNK_CONTRACTS * CONTRACT_XRP
                GRID_INITIAL_XRP = GRID_INITIAL_CONTRACTS * CONTRACT_XRP
                _applied_qty = int(_new_qty)
                _available_delta_usd = _post_cleanup_balance
                _stored_signal = _new_state["signal_direction"]
                st.success(
                    f"Signal-cycle quantity committed after cleanup: "
                    f"{_stored_signal}, {int(_new_qty)} XRP/order, "
                    f"available USD {float(_post_cleanup_balance):.4f}."
                )
        except Exception as _post_cleanup_exc:
            _qty_error = f"Post-cleanup quantity update failed; new grid blocked ({type(_post_cleanup_exc).__name__})."
            grid_cycle_ready = False
            st.error(_qty_error)

    # ------------------------------------------------------------
    # VISIBLE BALANCE -> QUANTITY EXPLANATION (DISPLAY ONLY)
    # Shows the actual Delta USD balance used by the sizing rule, the
    # quantity currently applied, and any pending preview without changing orders.
    # ------------------------------------------------------------
    with st.expander("💰 वास्तविक बैलेंस के अनुसार ऑर्डर मात्रा — देखने के लिए टैप करें", expanded=False):
        st.divider()
        st.subheader("💰 वास्तविक बैलेंस के अनुसार ऑर्डर मात्रा")
        st.caption(
            "मात्रा Delta Exchange India के authenticated USD available balance से तय होती है "
            "(USDT नहीं)। नई मात्रा केवल valid BUY/SELL signal और पुराने orders/position की "
            "सफल cleanup जाँच के बाद लागू होती है।"
        )
        _display_balance = _available_delta_usd if isinstance(_available_delta_usd, (int, float)) and math.isfinite(float(_available_delta_usd)) else None
        _display_qty = _applied_qty if isinstance(_applied_qty, (int, float)) and int(_applied_qty) >= 1 else None
        _preview_qty = None
        if _display_balance is not None:
            try:
                _preview_qty = _quantity_from_available_usd(_display_balance)
            except Exception:
                _preview_qty = None

        _qcols = st.columns(3)
        with _qcols[0]:
            st.metric("वास्तविक उपलब्ध USD बैलेंस", f"${_display_balance:,.4f}" if _display_balance is not None else "API से उपलब्ध नहीं")
        with _qcols[1]:
            st.metric("अभी लागू मात्रा / ऑर्डर", f"{int(_display_qty)} XRP" if _display_qty is not None else "अभी लागू नहीं")
        with _qcols[2]:
            st.metric("बैलेंस से निकली मात्रा", f"{int(_preview_qty)} XRP" if _preview_qty is not None else "गणना उपलब्ध नहीं")

        if _display_balance is not None and _preview_qty is not None:
            st.write(
                f"**हिसाब:** उपलब्ध बैलेंस **${_display_balance:,.4f} USD** → "
                f"नियम के अनुसार **{int(_preview_qty)} XRP प्रति ऑर्डर**।"
            )
            st.caption("नियम: $5 USD से कम बैलेंस पर 1 XRP; $5 या अधिक पर floor(available USD ÷ 2), न्यूनतम 1 XRP।")
            if _qty_signal_change_pending:
                st.warning(
                    f"नई signal quantity अभी केवल preview है: {int(_preview_qty)} XRP/order। "
                    "Cleanup और exchange verification पूरा होने तक इसे लागू नहीं माना जाएगा।"
                )
            elif _display_qty is not None and int(_display_qty) != int(_preview_qty):
                st.info(
                    f"अभी चल रही signal पर लागू मात्रा {int(_display_qty)} XRP/order है। "
                    f"मौजूदा balance से गणना {int(_preview_qty)} XRP है; signal बदलने और cleanup सफल होने पर ही मात्रा बदलेगी।"
                )
            elif _display_qty is not None:
                st.success(f"लागू मात्रा सत्यापित state के अनुसार {int(_display_qty)} XRP प्रति entry/re-entry order है।")

        st.write(f"**वर्तमान grid आकार:** {GRID_LEVEL_COUNT} स्तर × {GRID_CHUNK_XRP:.0f} XRP प्रति स्तर = अधिकतम नियोजित entry मात्रा {GRID_LEVEL_COUNT * GRID_CHUNK_XRP:.0f} XRP (यह अलग-अलग ऑर्डरों का कुल है, एक ऑर्डर नहीं)।")
        st.caption("वास्तविक order acceptance, margin और execution की पुष्टि Delta Exchange करता है; यह गणना execution की गारंटी नहीं है।")

    if (
        grid_direction
        and wrong_direction_cleanup_ok
        and grid_open_ok
        and grid_pos_ok
        and grid_cycle_ready
        and _startup_cleanup_ok
        and not _qty_error
    ):

        # --------------------------------------------------------
        # DIRECTION / ORDER SIDES
        # --------------------------------------------------------
        if grid_direction == "BUY":
            entry_side = "buy"
            target_side = "sell"
            reentry_side = "buy"
        else:
            entry_side = "sell"
            target_side = "buy"
            reentry_side = "sell"

        # Unique tag for THIS confirmed SuperTrend signal cycle.
        # It prevents a new cycle from being confused with old closed orders
        # at the same price.
        try:
            _cycle_epoch_tag = str(int(float(_signal_epoch_for_level)))
        except Exception:
            _cycle_epoch_tag = str(int(time.time()))
        _cycle_tag = _cycle_epoch_tag[-9:]

        # --------------------------------------------------------
        # STARTUP / FRESH-CYCLE: ARM ALL 13 LEVELS TOGETHER
        # --------------------------------------------------------
        # ALL 13 levels are submitted as LIMIT orders at their exact fixed
        # signal-time prices.  We do NOT convert crossed levels to MARKET.
        # A marketable LIMIT (for example BUY limit below/at CMP or SELL limit
        # above/at CMP) is allowed to fill immediately at the exchange, while
        # the remaining limits stay pending at their immutable level prices.
        # live_price is therefore NOT used to calculate or change any level.
        # A per-refresh/cycle action guard below also prevents duplicate placement
        # while Delta is still updating its open/closed order feeds.
        startup_armed_key = st.session_state.get("grid_startup_armed_cycle", "")
        # Prevent the same level from being attempted twice during one Streamlit refresh.
        grid_attempted_this_refresh = set()

        if startup_armed_key != new_cycle_key:
            grid_open_ok, grid_open_orders = _grid_fetch_orders()
            grid_closed_ok, grid_closed_orders = _grid_fetch_closed_orders()

            startup_results = []
            all_startup_ok = True

            # ========================================================
            # EXCHANGE-DECIDED MARGIN — NO CLIENT-SIDE MARGIN GATE
            # Attempt every fixed level in this cycle. Delta Exchange is the
            # authority for margin acceptance/rejection on each individual order.
            # A rejection for one level must not prevent later levels being sent.
            # ========================================================
            _submit_all_levels_to_delta = True

            if "grid_action_done" not in st.session_state:
                st.session_state["grid_action_done"] = set()
            _grid_action_done = st.session_state["grid_action_done"]

            if _submit_all_levels_to_delta:
                for level_no, price in pending_entry_levels:
                    price = float(price)
                    entry_label = f"ENTRY_{_cycle_tag}_L{level_no}"
                    entry_open = _grid_has_client_label(
                        grid_open_orders,
                        entry_label
                    )

                    # A startup entry is already considered armed if the same
                    # cycle/level has a filled historical order.
                    entry_filled = False
                    if grid_closed_ok:
                        for closed in grid_closed_orders:
                            if not _grid_is_bot(closed):
                                continue
                            if not _grid_order_was_filled(closed):
                                continue
                            if _grid_order_id(closed) and _grid_order_matches_label(closed, entry_label):
                                entry_filled = True
                                _grid_record_order_identity(
                                    str(closed.get("client_order_id", "")),
                                    _grid_order_id(closed),
                                    entry_label,
                                    grid_direction
                                )
                                break

                    if entry_open or entry_filled:
                        startup_results.append(
                            f"L{level_no}=ALREADY"
                        )
                        continue

                    # ALWAYS place the original fixed level as a LIMIT order.
                    # If the level is already crossed by CMP, Delta may fill this
                    # marketable LIMIT immediately. It is still the same fixed-level
                    # order, so its target remains tied to this exact level price.
                    _startup_action_key = f"STARTUP|{new_cycle_key}|L{level_no}"
                    if _startup_action_key in _grid_action_done:
                        startup_results.append(f"L{level_no}=ALREADY")
                        continue
                    if level_no in grid_attempted_this_refresh:
                        startup_results.append(f"L{level_no}=ALREADY")
                        continue
                    grid_attempted_this_refresh.add(level_no)

                    result = _grid_place_limit(
                        side=entry_side,
                        price=price,
                        label=entry_label,
                        direction=grid_direction,
                        size=GRID_CHUNK_CONTRACTS
                    )
                    mode = "LIMIT"

                    ok = (
                        isinstance(result, dict)
                        and result.get("success") is True
                    )

                    if ok:
                        _grid_action_done.add(_startup_action_key)
                        startup_results.append(
                            f"L{level_no}={mode}@{show_price(price)}"
                        )
                    else:
                        all_startup_ok = False
                        startup_results.append(
                            f"L{level_no}=FAILED"
                        )
                        _grid_log_event(account_id, level_no, "STARTUP_ENTRY", result, price)
                        st.error(
                            f"❌ Startup L{level_no} {mode} failed "
                            f"@ fixed level {show_price(price)}: {result}"
                        )

            if _submit_all_levels_to_delta:
                _armed_count = 0
                _failed_count = 0
                for _r in startup_results:
                    if "=FAILED" in str(_r):
                        _failed_count += 1
                    elif "=ALREADY" in str(_r) or "=" in str(_r):
                        # A successful _grid_place_limit() always carries a
                        # valid Delta order ID. Only those results reach here.
                        if "=FAILED" not in str(_r):
                            _armed_count += 1

                if _failed_count == 0 and _armed_count == GRID_LEVEL_COUNT:
                    st.session_state["grid_startup_armed_cycle"] = new_cycle_key
                    st.success(
                        f"✅ {GRID_LEVEL_COUNT} LEVELS PLANNED | {GRID_LEVEL_COUNT} ARMED | 0 FAILED | "
                        f"{grid_direction} | Fixed ATR anchor {show_price(grid_atr_line)}"
                    )
                else:
                    st.warning(
                        f"⚠️ {GRID_LEVEL_COUNT} LEVELS PLANNED | {_armed_count} ARMED | "
                        f"{_failed_count} FAILED | Delta Exchange response is final for each order"
                    )

            # Refresh immediately after the 25 startup submissions.
            grid_open_ok, grid_open_orders = _grid_fetch_orders()

        # --------------------------------------------------------
        def _grid_place_fixed_target(side, price, label, direction, size):
            """Place a fixed-level reduce-only target only when exchange position backs it.

            Target price is always derived from the immutable grid level.  Before
            submitting, re-read Delta's position and open orders so a delayed fill
            update cannot create a reduce-only target against a flat position, and
            existing targets cannot collectively exceed the available position.
            """
            # An already-live target is accepted only if every material field
            # matches. A same-label but wrong-price/side/size target must never be
            # silently treated as correct.
            open_ok, live_orders = _grid_fetch_orders()
            if not open_ok:
                return {"success": False, "error": "Cannot verify Delta open orders before target placement; will retry next refresh."}
            matching_targets = [
                _o for _o in live_orders
                if _grid_is_bot(_o) and _grid_is_limit(_o) and _grid_order_matches_label(_o, label)
            ]
            if len(matching_targets) > 1:
                return {
                    "success": False,
                    "error": (
                        f"Multiple open orders share target label {label}; "
                        "refusing to claim success or add another target. "
                        "Reconcile duplicate exchange orders first."
                    ),
                }
            if matching_targets:
                _o = matching_targets[0]
                _oid = _grid_order_id(_o)
                _existing_price = _grid_price(_o)
                _existing_size = _grid_contracts(_o)
                _existing_side = _grid_order_side(_o)
                _existing_ro_raw = _o.get("reduce_only", False)
                _existing_ro = (
                    _existing_ro_raw is True
                    or str(_existing_ro_raw).strip().lower() in {"true", "1", "yes"}
                )
                _same = (
                    bool(_oid)
                    and _existing_side == str(side).lower()
                    and _existing_price is not None
                    and abs(float(_existing_price) - float(price)) <= 0.01
                    and _existing_size == int(abs(float(size)))
                    and _existing_ro is True
                )
                if _same:
                    return {
                        "success": True,
                        "result": _o,
                        "id": _oid,
                        "order_id": _oid,
                        "client_order_id": _o.get("client_order_id", ""),
                        "verified_existing": True,
                    }

                # A mismatched open target is cancelled only when the API proves
                # that none of it has filled. If fill state is unknown/partial,
                # stop safely rather than risking a duplicate close order.
                _total, _filled, _unfilled = _grid_order_fill_quantities(_o)
                if not _oid or _filled is None or _filled > 1e-9:
                    return {
                        "success": False,
                        "error": (
                            f"Existing target {label} mismatches the fixed target and "
                            "its fill state is unknown/partial; refusing to duplicate "
                            "or cancel it automatically. "
                            f"Existing id={_oid}, side={_existing_side}, "
                            f"price={_existing_price}, size={_existing_size}, "
                            f"reduce_only={_existing_ro}; requested "
                            f"side={str(side).lower()}, price={price}, "
                            f"size={int(abs(float(size)))}, reduce_only=True."
                        ),
                    }
                _cancel_result = api.cancel_order(_oid)
                _verify_ok, _verified_orders = _grid_fetch_orders()
                if not _verify_ok:
                    return {
                        "success": False,
                        "error": (
                            f"Mismatched target {_oid} cancellation cannot be verified; "
                            "no replacement target was sent."
                        ),
                    }
                _still_live = any(
                    _grid_is_bot(_x) and _grid_order_matches_label(_x, label)
                    for _x in _verified_orders
                )
                if _still_live:
                    return {
                        "success": False,
                        "error": (
                            f"Mismatched target {_oid} is still listed by Delta after "
                            f"cancellation attempt ({_cancel_result}); replacement "
                            "held to avoid duplicates."
                        ),
                    }
                live_orders = _verified_orders

            pos_ok, pos, raw_pos_size = _grid_fetch_position()
            if not pos_ok:
                return {
                    "success": False,
                    "error": (
                        "Delta position verification failed; target is held safely "
                        "and will retry next refresh."
                    ),
                }
            try:
                pos_size = float(raw_pos_size or 0)
            except (TypeError, ValueError):
                pos_size = 0.0
            if abs(pos_size) < 1e-9:
                return {
                    "success": False,
                    "error": (
                        "waiting_for_position: Delta reports flat; target will retry "
                        "after the entry fill appears in the live position."
                    ),
                }

            # Delta position payload normally carries side. If not, use signed size
            # when available; do not guess when the direction cannot be established.
            _pos_side_raw = str(
                pos.get("side", pos.get("direction", "")) or ""
            ).strip().lower()
            if _pos_side_raw in {"buy", "long"}:
                _pos_direction = "BUY"
            elif _pos_side_raw in {"sell", "short"}:
                _pos_direction = "SELL"
            elif pos_size > 0:
                _pos_direction = "BUY"
            elif pos_size < 0:
                _pos_direction = "SELL"
            else:
                _pos_direction = ""
            if _pos_direction and _pos_direction != str(direction).upper():
                return {
                    "success": False,
                    "error": (
                        f"position_direction_mismatch: expected {direction}, "
                        f"Delta position is {_pos_direction}; target will retry "
                        "after reconciliation."
                    ),
                }

            try:
                requested = int(abs(float(size)))
            except (TypeError, ValueError):
                requested = 0
            if requested <= 0:
                return {
                    "success": False,
                    "error": "No confirmed executed contracts are available for this target.",
                }

            # Do not arm reduce-only targets whose aggregate size exceeds the live
            # position. Count only currently open GRID targets for this cycle.
            try:
                _cycle_tag_for_target = str(label).split("_")[1]
            except Exception:
                _cycle_tag_for_target = ""
            _reserved_target_contracts = 0
            for _o in live_orders:
                if not _grid_is_bot(_o) or not _grid_is_limit(_o):
                    continue
                _role, _level = _grid_client_role_level(_o)
                _cid = str(_o.get("client_order_id", ""))
                if _role != "TARGET":
                    continue
                if _cycle_tag_for_target and f"_{_cycle_tag_for_target}_" not in _cid:
                    continue
                _reserved_target_contracts += _grid_contracts(_o)

            _position_contracts = int(abs(pos_size))
            _available_target_contracts = max(
                0, _position_contracts - _reserved_target_contracts
            )
            if _available_target_contracts <= 0:
                return {
                    "success": False,
                    "error": (
                        f"waiting_for_target_capacity: live position={_position_contracts} "
                        f"contracts, already reserved by open targets={_reserved_target_contracts}; "
                        "will retry after exchange state updates."
                    ),
                }
            if _available_target_contracts < requested:
                return {
                    "success": False,
                    "error": (
                        f"waiting_for_target_capacity: target needs {requested} contracts, "
                        f"but only {_available_target_contracts} unreserved position "
                        "contracts are currently verified; no partial target was placed."
                    ),
                }

            result = _grid_place_limit(
                side=side,
                price=price,
                label=label,
                direction=direction,
                size=requested,
                reduce_only=True,
            )
            if isinstance(result, dict) and result.get("success") is True:
                return result

            # If Delta reports a reduce-only/position race, don't mark the lifecycle
            # complete. The next Streamlit refresh re-checks actual position and retries.
            return result
        # CLOSED ORDERS / LIVE ORDERS FOR LEVEL STATE
        # --------------------------------------------------------
        grid_open_ok, grid_open_orders = _grid_fetch_orders()
        grid_closed_ok, grid_closed_orders = _grid_fetch_closed_orders()

        def _closed_order_epoch(order):
            # ISO-8601 timestamps must be parsed as timestamps, not skipped in
            # favour of the numeric order ID; role sequencing depends on this.
            for key in ("updated_at", "filled_at", "executed_at", "closed_at", "created_at", "id"):
                value = order.get(key)
                if value in (None, ""):
                    continue
                try:
                    numeric = float(value)
                    if numeric > 10_000_000_000:
                        numeric /= 1000.0
                    return numeric
                except (TypeError, ValueError):
                    try:
                        parsed = pd.Timestamp(value)
                        if pd.isna(parsed):
                            continue
                        if parsed.tzinfo is None:
                            parsed = parsed.tz_localize("UTC")
                        return float(parsed.timestamp())
                    except Exception:
                        continue
            return 0.0

        startup_cycle_armed = (
            st.session_state.get("grid_startup_armed_cycle", "")
            == new_cycle_key
        )

        # --------------------------------------------------------
        # ONE INDEPENDENT CHAIN PER FIXED LEVEL
        # --------------------------------------------------------
        # ENTRY @ fixed level
        #   -> TARGET @ fixed level +/- GRID_STEP
        #   -> RE-ENTRY @ SAME fixed level
        #   -> TARGET @ SAME fixed target
        #   -> repeat
        #
        # Crucial startup rule:
        # A pending LIMIT entry gets NO target until that exact level fills.
        # If a fixed-price LIMIT fills immediately because it is marketable,
        # its target is still based on the fixed level, never on the live fill price.
        for level_no, price in pending_entry_levels:
            price = float(price)

            entry_label = f"ENTRY_{_cycle_tag}_L{level_no}"
            target_label = f"TARGET_{_cycle_tag}_L{level_no}"
            reentry_label = f"REENTRY_{_cycle_tag}_L{level_no}"

            target_price = _target_from_original_level(
                level_no,
                grid_direction
            )

            entry_open = _grid_has_client_label(
                grid_open_orders,
                entry_label
            )
            target_open = _grid_has_client_label(
                grid_open_orders,
                target_label
            )
            reentry_open = _grid_has_client_label(
                grid_open_orders,
                reentry_label
            )

            latest_role = ""
            latest_epoch = -1.0
            latest_order_id = ""
            _fallback_order = None  # NEW: holds direct lookup result if used

            if grid_closed_ok:
                for closed in grid_closed_orders:
                    if not _grid_is_bot(closed):
                        continue
                    if not _grid_order_was_filled(closed):
                        continue

                    order_id = _grid_order_id(closed)
                    if not order_id:
                        # A closed order without Delta's actual exchange ID is
                        # not a confirmed order instance.
                        continue

                    cid = str(closed.get("client_order_id", ""))
                    epoch = _closed_order_epoch(closed)

                    matched_role = ""
                    matched_label = ""
                    if _grid_order_matches_label(closed, entry_label):
                        matched_role = "ENTRY"
                        matched_label = entry_label
                    elif _grid_order_matches_label(closed, target_label):
                        matched_role = "TARGET"
                        matched_label = target_label
                    elif _grid_order_matches_label(closed, reentry_label):
                        matched_role = "REENTRY"
                        matched_label = reentry_label

                    if matched_role and epoch >= latest_epoch:
                        _grid_record_order_identity(
                            cid, order_id, matched_label, grid_direction
                        )
                        latest_role = matched_role
                        latest_epoch = epoch
                        latest_order_id = order_id

            # A still-open entry with zero executed contracts is only pending;
            # it must not get a target. If Delta reports a partial fill while
            # the remainder is still open, treat the filled portion as an ENTRY
            # event and place its fixed-level target instead of silently waiting.
            _active_entry_order = next((
                o for o in grid_open_orders
                if _grid_is_bot(o) and _grid_order_matches_label(o, entry_label)
            ), None)
            _active_entry_filled = (
                _grid_order_filled_contracts(_active_entry_order)
                if _active_entry_order else 0.0
            )
            if entry_open and _active_entry_filled <= 0:
                continue
            if entry_open and _active_entry_filled > 0:
                _active_entry_id = _grid_order_id(_active_entry_order)
                _active_entry_epoch = _grid_order_event_epoch(_active_entry_order)
                if _active_entry_epoch >= latest_epoch:
                    latest_role = "ENTRY"
                    latest_epoch = _active_entry_epoch
                    latest_order_id = _active_entry_id or f"partial-L{level_no}"

            # One action per exact level-state. This is deliberately based on
            # the latest filled role/epoch, not just price or label. It prevents
            # duplicate target/re-entry placement when Delta's open/closed feeds
            # lag for a moment after an order is submitted.
            if "grid_action_done" not in st.session_state:
                st.session_state["grid_action_done"] = set()
            _grid_action_done = st.session_state["grid_action_done"]
            _state_action_key = (
                f"{new_cycle_key}|L{level_no}|{latest_role}|"
                f"{latest_order_id or latest_epoch}"
            )

            # If the exact level has never filled in this cycle, arm/repair
            # its fixed entry. This also recovers after a Streamlit restart.
            if latest_role == "":
                # Startup already attempted this level in this same refresh.
                if level_no in grid_attempted_this_refresh:
                    continue

                if startup_cycle_armed:
                    # FIX: previously this just did `continue` forever once the
                    # cycle was marked "25 ARMED", even if bulk closed-order
                    # pagination never caught an instantly-filled marketable
                    # LIMIT (common for upper levels placed above live CMP).
                    # NEW: do ONE direct per-order lookup by client_order_id
                    # before giving up, instead of trusting only bulk pages.
                    _entry_identity = _grid_identity_for_label(entry_label)
                    if _entry_identity and _entry_identity.get("client_order_id"):
                        _lookup_ok, _fallback_order = _grid_fetch_order_by_client_id(
                            _entry_identity["client_order_id"]
                        )
                        if not _lookup_ok:
                            _fallback_order = None

                    if _fallback_order and _grid_order_was_filled(_fallback_order):
                        latest_role = "ENTRY"
                        latest_order_id = (
                            _grid_order_id(_fallback_order)
                            or _entry_identity.get("order_id")
                        )
                        latest_epoch = _grid_order_event_epoch(_fallback_order)
                        _grid_log_event(
                            account_id, level_no, "ENTRY_FALLBACK_VERIFIED",
                            f"Direct client_order_id lookup confirmed fill for {entry_label}",
                            price
                        )
                        # Deliberately NOT continuing here — falls through to
                        # the ENTRY handling block below so its target gets built.
                    else:
                        # Genuinely still unfilled/open per direct exchange
                        # check, or identity/lookup unavailable. Never guess;
                        # retry again on the next refresh.
                        continue
                else:
                    if (
                        not target_open
                        and not reentry_open
                        and _state_action_key not in _grid_action_done
                    ):
                        # Recovery uses the SAME immutable fixed level as a LIMIT.
                        # A marketable LIMIT may fill immediately; it is never
                        # changed into a MARKET order and never recalculated
                        # from live CMP.
                        result = _grid_place_limit(
                            side=entry_side,
                            price=price,
                            label=entry_label,
                            direction=grid_direction,
                            size=GRID_CHUNK_CONTRACTS
                        )
                        mode = "LIMIT"

                        if not (
                            isinstance(result, dict)
                            and result.get("success") is True
                        ):
                            _grid_log_event(account_id, level_no, "ENTRY_RETRY", result, price)
                            st.error(
                                f"❌ L{level_no} entry retry failed "
                                f"({mode}) @ {show_price(price)}: {result}"
                            )
                        else:
                            _grid_action_done.add(_state_action_key)
                    continue

            # Entry filled -> identify the level by its fixed client label and
        # build the target from the immutable fixed level, never from fill price.

            if latest_role == "ENTRY":
                if (
                    not target_open
                    and not reentry_open
                    and _state_action_key not in _grid_action_done
                ):
                    # Match the target size to the actual entry execution, not
                    # blindly to the configured chunk when an entry partially fills.
                    _entry_fill_qty = 0.0
                    _entry_search_rows = list(grid_open_orders) + list(grid_closed_orders)
                    if _fallback_order:
                        # NEW: include the direct lookup result so a fallback-
                        # verified fill's quantity is not missed just because
                        # it wasn't present in the bulk open/closed pages.
                        _entry_search_rows.append(_fallback_order)
                    for _entry_row in _entry_search_rows:
                        if _grid_order_id(_entry_row) == str(latest_order_id) and _grid_order_matches_label(_entry_row, entry_label):
                            _entry_fill_qty = _grid_order_filled_contracts(_entry_row)
                            break
                    _target_qty = int(round(_entry_fill_qty))
                    if _target_qty <= 0:
                        # A closed-order status alone is not enough to invent a
                        # target quantity. Wait until Delta's fill endpoint
                        # confirms executed contracts.
                        continue
                    result = _grid_place_fixed_target(
                        side=target_side,
                        price=target_price,
                        label=target_label,
                        direction=grid_direction,
                        size=_target_qty
                    )
                    if not (
                        isinstance(result, dict)
                        and result.get("success") is True
                    ):
                        _target_error = str(result.get("error", result)) if isinstance(result, dict) else str(result)
                        _grid_log_event(account_id, level_no, "TARGET_ENTRY", _target_error, target_price)
                        if any(_w in _target_error.lower() for _w in ("waiting_for_position", "waiting_for_target_capacity", "position_direction_mismatch", "position verification failed", "open orders before target")):
                            st.warning(f"⏳ L{level_no} target is waiting for verified Delta position/state at fixed price {show_price(target_price)}. It will retry on refresh. Details: {_target_error}")
                        else:
                            st.error(f"❌ L{level_no} target failed @ fixed target {show_price(target_price)}: {result}")
                    else:
                        _grid_action_done.add(_state_action_key)

            # Target filled -> re-entry goes back to THE SAME fixed level.
            elif latest_role == "TARGET":
                # Additive notification for an exchange-confirmed filled target.
                _target_event_key = f"{new_cycle_key}|L{level_no}|TARGET|{latest_order_id or latest_epoch}"
                _discord_target_events = st.session_state.setdefault("_discord_target_events", set())
                _discord_level_last_fill = st.session_state.setdefault("_discord_level_last_fill", {})
                _discord_target_type = "REENTRY_TARGET" if _discord_level_last_fill.get(f"{new_cycle_key}|L{level_no}") == "REENTRY" else "TARGET"
                if _target_event_key not in _discord_target_events:
                    _discord_target_events.add(_target_event_key)
                    _discord_count_type = "REENTRY_TARGET" if _discord_target_type == "REENTRY_TARGET" else "TARGET"
                    _discord_cycle_counts = _discord_register_cycle_event(new_cycle_key, _discord_count_type, _target_event_key)
                    _discord_target_hit(
                        SYMBOL, level_no, target_price, live_price,
                        GRID_CHUNK_CONTRACTS, _format_epoch(latest_epoch), _discord_target_type,
                        new_cycle_key, _discord_cycle_counts
                    )
                _discord_level_last_fill[f"{new_cycle_key}|L{level_no}"] = "TARGET"
                if (
                    not reentry_open
                    and not target_open
                    and _state_action_key not in _grid_action_done
                ):
                    result = _grid_place_limit(
                        side=reentry_side,
                        price=price,
                        label=reentry_label,
                        direction=grid_direction,
                        size=GRID_CHUNK_CONTRACTS
                    )
                    if not (
                        isinstance(result, dict)
                        and result.get("success") is True
                    ):
                        st.error(
                            f"❌ L{level_no} re-entry failed "
                            f"@ fixed level {show_price(price)}: {result}"
                        )
                    else:
                        _grid_action_done.add(_state_action_key)

            # Re-entry filled -> same fixed target again.
            elif latest_role == "REENTRY":
                _reentry_event_key = f"{new_cycle_key}|L{level_no}|REENTRY|{latest_order_id or latest_epoch}"
                _discord_seen_reentries = st.session_state.setdefault("_discord_seen_reentries", set())
                if _reentry_event_key not in _discord_seen_reentries:
                    _discord_seen_reentries.add(_reentry_event_key)
                    _discord_cycle_counts = _discord_register_cycle_event(new_cycle_key, "REENTRY", _reentry_event_key)
                    _discord_reentry(
                        SYMBOL, reentry_side.upper(), level_no, price,
                        GRID_CHUNK_CONTRACTS, _format_epoch(latest_epoch),
                        new_cycle_key, _discord_cycle_counts
                    )
                st.session_state.setdefault("_discord_level_last_fill", {})[f"{new_cycle_key}|L{level_no}"] = "REENTRY"
                if (
                    not target_open
                    and not reentry_open
                    and _state_action_key not in _grid_action_done
                ):
                    _reentry_fill_qty = 0.0
                    for _reentry_row in (grid_open_orders + grid_closed_orders):
                        if _grid_order_id(_reentry_row) == str(latest_order_id) and _grid_order_matches_label(_reentry_row, reentry_label):
                            _reentry_fill_qty = _grid_order_filled_contracts(_reentry_row)
                            break
                    _target_qty = int(round(_reentry_fill_qty))
                    if _target_qty <= 0:
                        # Do not guess executed quantity; wait for Delta fills.
                        continue
                    result = _grid_place_fixed_target(
                        side=target_side,
                        price=target_price,
                        label=target_label,
                        direction=grid_direction,
                        size=_target_qty
                    )
                    if not (
                        isinstance(result, dict)
                        and result.get("success") is True
                    ):
                        _target_error = str(result.get("error", result)) if isinstance(result, dict) else str(result)
                        _grid_log_event(account_id, level_no, "TARGET_REENTRY", _target_error, target_price)
                        if any(_w in _target_error.lower() for _w in ("waiting_for_position", "waiting_for_target_capacity", "position_direction_mismatch", "position verification failed", "open orders before target")):
                            st.warning(f"⏳ L{level_no} target is waiting for verified Delta position/state at fixed price {show_price(target_price)}. It will retry on refresh. Details: {_target_error}")
                        else:
                            st.error(f"❌ L{level_no} target retry failed @ fixed target {show_price(target_price)}: {result}")
                    else:
                        _grid_action_done.add(_state_action_key)

            # Pull both feeds after lifecycle actions. This keeps the following
            # chart/report pass tied to Delta's latest exchange state.
            grid_open_ok, grid_open_orders = _grid_fetch_orders()
            grid_closed_ok, grid_closed_orders = _grid_fetch_closed_orders()


    # ------------------------------------------------------------
    # DELTA EXCHANGE ORDER REPORT — OPEN + FILLED/CLOSED
    # ------------------------------------------------------------
    # This report is intentionally sourced from Delta itself, not from
    # Streamlit session state.  Therefore a LIMIT, MARKET, FILLED, CANCELLED
    # or REJECTED order can be seen even after the open-order list changes.
    grid_open_ok, grid_open_orders = _grid_fetch_orders()
    grid_closed_ok, grid_closed_orders = _grid_fetch_closed_orders()

    # Discord notifications are event-only: do not publish the existing order
    # history/snapshot when the dashboard is opened or refreshed. Monitor only
    # actual live-price crossings of currently open Delta orders observed across
    # consecutive refreshes in this active dashboard session.
    try:
        if grid_open_ok:
            _discord_check_live_order_touches(grid_open_orders, live_price)
        if grid_open_ok or grid_closed_ok:
            _discord_check_order_lifecycle(
                grid_open_orders if grid_open_ok else [],
                grid_closed_orders if grid_closed_ok else [],
                live_price,
            )
    except Exception as _discord_touch_exc:
        print(f"Discord live order lifecycle/touch alerts skipped: {type(_discord_touch_exc).__name__}")

    # ------------------------------------------------------------
    # REAL EXCHANGE ORDER BLOCKS + TARGET TRADE P/L (DISPLAY ONLY)
    # Open orders are always listed separately. Closed target trades are paired
    # only by exchange order tags and actual fills; P/L is not guessed.
    # ------------------------------------------------------------
    try:
        _report_fills_ok, _report_fills = _grid_fetch_fills()
        _report_open_blocks, _report_closed_pairs = _grid_trade_report_rows(
            grid_open_orders if grid_open_ok else [],
            grid_closed_orders if grid_closed_ok else [],
            _report_fills if _report_fills_ok else [],
        )
        # Discord target-completion alert: first observation is baseline-only,
        # so old closed records are never replayed when this dashboard opens.
        _target_pair_ids = {str(x["exit"].get("id", x["exit"].get("order_id", ""))) for x in _report_closed_pairs}
        _target_pair_ids.discard("")
        _target_seen_key = "_discord_seen_closed_target_trade_ids"
        if _target_seen_key not in st.session_state:
            st.session_state[_target_seen_key] = set(_target_pair_ids)
        else:
            _already_seen_targets = st.session_state[_target_seen_key]
            for _trade in reversed(_report_closed_pairs):
                _exit_id = str(_trade["exit"].get("id", _trade["exit"].get("order_id", "")))
                if not _exit_id or _exit_id in _already_seen_targets:
                    continue
                _net = _trade["net"]
                _discord_alert(
                    f"TARGET_FILLED_NET_PNL_{_exit_id}",
                    "🎯 TARGET HIT & EXECUTED — ENTRY + EXIT + NET PROFIT",
                    3066993 if _net is not None and _net >= 0 else 15158332,
                    [
                        _discord_field("🎯 TARGET STATUS", "Exchange record confirms target/exit execution", inline=False),
                        _discord_field("ENTRY PRICE", _decimal_7(_trade["entry_price"])),
                        _discord_field("EXIT / TARGET PRICE", _decimal_7(_trade["exit_price"])),
                        _discord_field("Entry Order ID", _trade["entry"].get("id", _trade["entry"].get("order_id", "-"))),
                        _discord_field("Target Order ID", _exit_id),
                        _discord_field("Gross Realized P/L", _decimal_7(_trade["gross"])),
                        _discord_field("Entry + Exit Actual Fees", _decimal_7(_trade["fees"])),
                        _discord_field("💰 NET PROFIT / LOSS AFTER FEES · 7 DECIMALS", _decimal_7(_net, "UNAVAILABLE — actual exchange P/L/fees missing"), inline=False),
                        _discord_field("Fee Currency", _trade["fee_currency"], inline=False),
                        _discord_field("Order Tag", _trade["exit"].get("client_order_id", _trade["exit"].get("client_id", "-")), inline=False),
                    ],
                    "Delta Exchange India • actual closed target + fills only",
                )
                _already_seen_targets.add(_exit_id)
            st.session_state[_target_seen_key] = _already_seen_targets
        with st.expander("📂 वास्तविक Exchange Orders और Target Profit — देखने के लिए टैप करें", expanded=False):
            st.divider()
            st.subheader("📂 वास्तविक Exchange Orders और Target Profit")
            with st.expander(f"🟦 OPEN ORDERS — {len(_report_open_blocks)} खुले ऑर्डर", expanded=False):
                if not grid_open_ok:
                    st.warning("Delta open-orders रिकॉर्ड उपलब्ध नहीं हैं; अभी कोई अनुमानित सूची नहीं दिखाई जा रही।")
                elif not _report_open_blocks:
                    st.info("अभी Delta Exchange पर कोई खुला ऑर्डर नहीं मिला।")
                else:
                    for _oi, _o in enumerate(_report_open_blocks, 1):
                        st.markdown(
                            f"<div style='border:1px solid #2563eb;border-left:6px solid #38bdf8;"
                            f"border-radius:10px;padding:12px;margin:8px 0;background:#0b1728;color:#e5e7eb'>"
                            f"<div style='font-weight:900;color:#7dd3fc'>🟦 OPEN ORDER #{_oi} · {_o['side']} {_o['type']} · {_o['state']}</div>"
                            f"<div style='margin-top:6px'>Order ID: <b>{_o['id']}</b> · Price: <b>{_decimal_7(_o['price'])}</b></div>"
                            f"<div>Qty: <b>{_decimal_7(_o['qty'])}</b> · Filled: <b>{_decimal_7(_o['filled'], '0.0000000')}</b> · Remaining: <b>{_decimal_7(_o['remaining'])}</b></div>"
                            f"<div style='font-size:12px;color:#cbd5e1;margin-top:5px'>Type: {_o['role']} · Tag: {_o['tag'] or '-'}</div></div>",
                            unsafe_allow_html=True,
                        )
            with st.expander(f"🎯 TARGET HIT — ENTRY + EXIT + FEES + NET P/L ({len(_report_closed_pairs)} रिकॉर्ड)", expanded=False):
                if not grid_closed_ok:
                    st.warning("Delta closed-order history उपलब्ध नहीं है; closed-trade profit नहीं दिखाया जा सकता।")
                elif not _report_closed_pairs:
                    st.info("अभी कोई completed target trade pair नहीं मिला। Delta के वास्तविक closed orders/fills उपलब्ध हों, और ENTRY/REENTRY तथा TARGET client tags में एक ही grid level (L1, L2...) मिलना चाहिए। कोई अनुमानित profit नहीं दिखाया गया।")
                    if grid_closed_ok and grid_closed_orders:
                        _debug_role_counts = {}
                        for _dbg_order in grid_closed_orders:
                            _dbg_tag = str(_dbg_order.get("client_order_id", _dbg_order.get("client_id", _dbg_order.get("label", ""))) or "")
                            _dbg_upper = _dbg_tag.upper()
                            _dbg_role = "TARGET" if "TARGET" in _dbg_upper or "_TGT_" in _dbg_upper or "_TP_" in _dbg_upper else "REENTRY" if "REENTRY" in _dbg_upper or "_RE_" in _dbg_upper else "ENTRY" if "ENTRY" in _dbg_upper or "_ENT_" in _dbg_upper else "OTHER"
                            _debug_role_counts[_dbg_role] = _debug_role_counts.get(_dbg_role, 0) + 1
                        st.caption("वास्तविक closed-order tag जाँच: " + " · ".join(f"{k}: {v}" for k, v in sorted(_debug_role_counts.items())))
                else:
                    for _ti, _t in enumerate(_report_closed_pairs, 1):
                        _net = _t['net']
                        _net_color = '#22c55e' if _net is not None and _net > 0 else '#f87171' if _net is not None and _net < 0 else '#fbbf24'
                        _gross_text = _decimal_7(_t['gross'])
                        _fees_text = _decimal_7(_t['fees'])
                        _net_text = _decimal_7(_net, 'UNAVAILABLE — Exchange P/L या दोनों वास्तविक fees नहीं मिलीं')
                        st.markdown(
                            f"<div style='border:1px solid #334155;border-left:6px solid {_net_color};"
                            f"border-radius:12px;padding:14px;margin:10px 0;background:#111827;color:#f8fafc'>"
                            f"<div style='font-weight:900;font-size:16px;color:#c4b5fd'>🎯 TARGET TRADE #{_ti} · CLOSED</div>"
                            f"<div style='margin-top:8px;display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px'>"
                            f"<div style='padding:9px;background:#172033;border-radius:7px'><small>ENTRY PRICE</small><div style='font-weight:900'>{_decimal_7(_t['entry_price'])}</div><small>Order {_t['entry'].get('id', _t['entry'].get('order_id','-'))}</small></div>"
                            f"<div style='padding:9px;background:#172033;border-radius:7px'><small>EXIT / TARGET PRICE</small><div style='font-weight:900'>{_decimal_7(_t['exit_price'])}</div><small>Order {_t['exit'].get('id', _t['exit'].get('order_id','-'))}</small></div>"
                            f"<div style='padding:9px;background:#172033;border-radius:7px'><small>GROSS REALIZED P/L</small><div style='font-weight:900'>{_gross_text}</div></div>"
                            f"<div style='padding:9px;background:#172033;border-radius:7px'><small>ENTRY + EXIT FEES</small><div style='font-weight:900'>{_fees_text}</div><small>{_t['fee_currency']}</small></div>"
                            f"</div><div style='margin-top:10px;padding:11px;background:#0b1220;border-radius:8px'>"
                            f"<div style='font-size:11px;color:#9ca3af'>NET PROFIT / LOSS AFTER ACTUAL FEES · 7 DECIMAL PLACES</div>"
                            f"<div style='font-size:20px;font-weight:900;color:{_net_color}'>{_net_text}</div></div>"
                            f"<div style='font-size:11px;color:#94a3b8;margin-top:8px'>Entry tag: {str(_t['entry'].get('client_order_id', _t['entry'].get('client_id','-')))[:90]}<br>Target tag: {str(_t['exit'].get('client_order_id', _t['exit'].get('client_id','-')))[:90]}</div></div>",
                            unsafe_allow_html=True,
                        )
            st.caption("सभी कीमतें/फीस Exchange records से ली जाती हैं। जहाँ realized P/L या entry/exit की वास्तविक fees उपलब्ध नहीं हैं, वहाँ Net P/L को UNAVAILABLE दिखाया जाएगा—कोई नकली लाभ नहीं बनाया जाएगा।")
    except Exception as _trade_report_exc:
        st.warning(f"Exchange order/profit display अभी उपलब्ध नहीं है: {type(_trade_report_exc).__name__}")

    # A target chain depends on reliable execution history. Show the exact
    # private fills endpoint error to the operator instead of hiding it.
    if _grid_fills_cache.get("loaded") and not _grid_fills_cache.get("ok"):
        st.error(
            "⚠️ DELTA FILLS API FAILED — automatic ENTRY → TARGET / TARGET → RE-ENTRY "
            "verification may be incomplete. Error: "
            f"{_grid_fills_cache.get('error') or 'Unknown fills API error'}"
        )

    # ------------------------------------------------------------
    # LIVE POSITION
    # ------------------------------------------------------------
    with st.expander("📍 LIVE GRID POSITION — विवरण देखने के लिए टैप करें", expanded=False):
        if grid_pos_ok:
            st.subheader("📍 LIVE GRID POSITION")
            direction_text = "LONG / BUY" if grid_position_size > 0 else ("SHORT / SELL" if grid_position_size < 0 else "FLAT")
            st.write(
                f"Position: **{direction_text}** | "
                f"Contracts: **{abs(int(grid_position_size))}** | "
                f"XRP: **{abs(grid_position_size) * CONTRACT_XRP:.3f} XRP**"
            )


    # ------------------------------------------------------------
    with st.expander("📡 GRID SUMMARY — विवरण देखने के लिए टैप करें", expanded=False):
        # CURRENT SIGNAL / GRID SUMMARY
        # ------------------------------------------------------------
        st.divider()
        st.subheader("📡 GRID SUMMARY")
        st.write(f"SuperTrend direction: **{grid_direction or '-'}**")
        st.write(f"Signal price: **{show_price(grid_signal_base)}**")
        st.write(f"ATR line: **{show_price(grid_atr_line)}**")
        st.write(f"Fixed grid: **{GRID_LEVEL_COUNT} levels × {GRID_STEP:.4f} USDT gap** | Anchor L1: **{show_price(grid_atr_line)}**")
        st.write(f"All fixed levels armed: **{GRID_LEVEL_COUNT}** | L1 ATR anchor: **{show_price(grid_atr_line)}**")
        st.write(f"GRID QUANTITY PER ORDER: **{GRID_CHUNK_XRP:.3f} XRP** ({GRID_CHUNK_CONTRACTS} contracts)")
        st.caption("PENDING ENTRY / TARGET / RE-ENTRY — sab isi quantity par chalenge.")
        st.write(f"Startup: **{GRID_LEVEL_COUNT} levels × {GRID_CHUNK_XRP:.3f} XRP** | Per-level quantity: **{GRID_CHUNK_XRP:.3f} XRP**")
        st.write(f"Startup mode: **ALL {GRID_LEVEL_COUNT} fixed levels = LIMIT** | Marketable crossed levels may **fill immediately** | Uncrossed levels remain **LIMIT**")
        st.write("**No later order is chased by live price. Every real filled order is matched to its own fixed level ID. Target is always calculated from that level, never from the actual fill/CMP. Direction change cancels the old grid and starts a new ATR-anchored cycle.**")
        st.write("**Chart: 🟢 GREEN = current valid pending ENTRY/RE-ENTRY order | 🔴 RED = current valid TARGET order. Wrong/unknown exchange transactions are cancelled first and are not drawn as valid grid lines.**")

        st.divider()
        st.subheader("🧾 PERSISTENT GRID ERROR/WARNING LOG (SURVIVES REFRESH)")
        _persist_log_rows = st.session_state.get("grid_error_log", [])
        if _persist_log_rows:
            st.dataframe(pd.DataFrame(_persist_log_rows[::-1]), use_container_width=True, hide_index=True)
        else:
            st.info("Abhi tak koi error/warning log nahi hua.")




    # ============================================================
    # ADDITIVE EXCHANGE POSITION + PNL SUMMARY PANEL
    # FAST-REFRESH FRAGMENT (1 SECOND) — DISPLAY ONLY.
    # Wrapping in @st.fragment(run_every=1) makes ONLY this panel
    # re-run every 1 second, independent of the slow full-page
    # refresh loop (candles / 16-level grid engine / order pagination
    # are NOT re-run by this fragment). No order/target/grid logic
    # is changed here.
    # ============================================================
    @st.fragment(run_every=1)
    def _panel_pnl_fast_fragment():
        _panel_position = grid_position if isinstance(grid_position, dict) else {}
        _panel_size = number(_panel_position.get("size"), 0.0) or 0.0
        _panel_side = (
            "LONG / BUY" if _panel_size > 0
            else "SHORT / SELL" if _panel_size < 0
            else "FLAT — NO OPEN POSITION"
        )
        _panel_qty_btc = abs(_panel_size) * CONTRACT_XRP

        # FIX: Delta's plain /v2/positions endpoint (grid_position above, used
        # by the trading engine) never returns mark_price, unrealized_pnl, or
        # realized_pnl - that is why this panel always showed "UNAVAILABLE".
        # The already-existing margined_positions() method calls
        # /v2/positions/margined, which does return these fields. This fetch is
        # for THIS DISPLAY PANEL ONLY; grid_position / grid_position_size used
        # by the trading engine above are not changed.
        _panel_margined_response = api.margined_positions()
        _panel_margined_data = _grid_result(_panel_margined_response)
        if isinstance(_panel_margined_data, list):
            _panel_margined_rows = _panel_margined_data
        elif isinstance(_panel_margined_data, dict):
            _panel_margined_rows = [_panel_margined_data]
        else:
            _panel_margined_rows = []
        _panel_margined_position = next(
            (
                _row for _row in _panel_margined_rows
                if isinstance(_row, dict) and int(float(_row.get("size", 0) or 0)) != 0
            ),
            {},
        )

        _panel_entry = number(
            _panel_margined_position.get(
                "entry_price",
                _panel_position.get("entry_price", _panel_position.get("average_entry_price"))
            )
        )
        _panel_mark = number(_panel_margined_position.get("mark_price"))
        _panel_unrealized = number(
            _panel_margined_position.get(
                "unrealized_pnl", _panel_margined_position.get("unrealised_pnl")
            )
        )
        _panel_realized = number(
            _panel_margined_position.get(
                "realized_pnl", _panel_margined_position.get("realised_pnl")
            )
        )
        _panel_pnl_color = (
            "#16a34a" if (_panel_unrealized or 0) > 0
            else "#ef4444" if (_panel_unrealized or 0) < 0
            else "#e5e7eb"
        )
        _panel_position_html = f"""
        <div style="width:100%;box-sizing:border-box;margin:10px 0 16px;padding:16px 18px;
            border:1px solid #334155;border-left:5px solid #38bdf8;border-radius:14px;
            background:linear-gradient(120deg,#0b1220,#172033);color:#e5e7eb;
            font-family:Arial,sans-serif;">
          <div style="font-size:18px;font-weight:900;color:#7dd3fc;margin-bottom:12px;">
            📊 LIVE EXCHANGE POSITION &amp; PNL
          </div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;">
            <div style="padding:11px;border-radius:9px;background:#111827;">
              <div style="font-size:11px;color:#9ca3af;">POSITION / SIDE</div>
              <div style="font-size:17px;font-weight:900;color:{'#22c55e' if _panel_size > 0 else '#f87171' if _panel_size < 0 else '#e5e7eb'};margin-top:5px;">{_panel_side}</div>
            </div>
            <div style="padding:11px;border-radius:9px;background:#111827;">
              <div style="font-size:11px;color:#9ca3af;">EXCHANGE POSITION QTY</div>
              <div style="font-size:20px;font-weight:900;margin-top:5px;">{abs(_panel_size):,.0f} contracts</div>
              <div style="font-size:12px;color:#cbd5e1;margin-top:3px;">{_panel_qty_btc:.3f} XRP</div>
            </div>
            <div style="padding:11px;border-radius:9px;background:#111827;">
              <div style="font-size:11px;color:#9ca3af;">MARK / CURRENT PRICE</div>
              <div style="font-size:18px;font-weight:900;margin-top:5px;">{show_price(_panel_mark) if _panel_mark is not None else 'UNAVAILABLE'}</div>
            </div>
            <div style="padding:11px;border-radius:9px;background:#111827;">
              <div style="font-size:11px;color:#9ca3af;">UNREALIZED PNL (EXCHANGE)</div>
              <div style="font-size:20px;font-weight:900;color:{_panel_pnl_color};margin-top:5px;">{f'{_panel_unrealized:,.4f}' if _panel_unrealized is not None else 'UNAVAILABLE'}</div>
            </div>
            <div style="padding:11px;border-radius:9px;background:#111827;">
              <div style="font-size:11px;color:#9ca3af;">REALIZED PNL (EXCHANGE)</div>
              <div style="font-size:18px;font-weight:900;margin-top:5px;">{f'{_panel_realized:,.4f}' if _panel_realized is not None else 'UNAVAILABLE'}</div>
            </div>
            <div style="padding:11px;border-radius:9px;background:#111827;">
              <div style="font-size:11px;color:#9ca3af;">AVERAGE ENTRY PRICE</div>
              <div style="font-size:18px;font-weight:900;margin-top:5px;">{show_price(_panel_entry)}</div>
            </div>
          </div>
          <div style="font-size:11px;color:#94a3b8;margin-top:10px;">
            Values are read from the selected Delta Exchange account's position API; no estimates are substituted for missing PNL fields. This panel refreshes every 1 second.
          </div>
        </div>
        """
        st.markdown(_panel_position_html, unsafe_allow_html=True)

    with st.expander("📍 LIVE GRID POSITION + REAL P/L — देखने के लिए टैप करें", expanded=False):
        _panel_pnl_fast_fragment()

    # ============================================================
    # PART 4/4 — REAL DELTA CANDLE CHART + SUPERTREND + GRID
    # ============================================================
    # Display only:
    # - Uses the SAME completed 1-hour Delta candles already loaded in df.
    # - Does not change order placement / target / re-entry logic.
    # - STARTUP arms all 13 fixed levels as LIMIT orders; marketable levels may fill immediately.
    # - Every grid level uses the configured GRID QUANTITY PER ORDER.
    # - Upper grid = RED, lower grid = GREEN.
    # - SuperTrend line = GREEN on BUY direction, RED on SELL direction.
    # - Prices and order quantities are written directly on the chart.
    # ============================================================

    now_ist = datetime.now(timezone.utc).astimezone(IST)
    st.write(f"Dashboard Time: **{now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}**")

    # Server-side strategy data remains COMPLETED-CANDLE ONLY.
    # The chart independently loads the real current/incomplete Delta candle
    # through Delta's public candle API + candlestick WebSocket below.
    # Therefore the live candle never enters SuperTrend/order calculations.
    _chart_df = df.tail(80).copy()

    _chart_times = [
        datetime.fromtimestamp(int(v), tz=timezone.utc).astimezone(IST).strftime("%Y-%m-%d %H:%M")
        for v in _chart_df["time"].tolist()
    ]
    _chart_open = [float(v) for v in _chart_df["open"].tolist()]
    _chart_high = [float(v) for v in _chart_df["high"].tolist()]
    _chart_low = [float(v) for v in _chart_df["low"].tolist()]
    _chart_close = [float(v) for v in _chart_df["close"].tolist()]

    # SuperTrend is already calculated by the existing engine.
    _chart_st = [None if pd.isna(v) else float(v) for v in _chart_df["SUPERTREND"].tolist()]
    _chart_dir = [int(v) for v in _chart_df["ST_DIRECTION"].tolist()]

    # TSL uses the SAME confirmed SuperTrend line already used by the strategy.
    # It is display-only: no order/trading logic is changed here.
    _chart_tsl_price = float(current_supertrend)
    _chart_tsl_color = "#22c55e" if current_trend == -1 else "#ef5350"
    _chart_tsl_direction = "BUY" if current_trend == -1 else "SELL"


    # LIVE ORDER LINES — REAL DELTA ORDERS ONLY
    # ------------------------------------------------------------
    # IMPORTANT: The chart never invents a grid/target/re-entry line.
    # It displays only orders that are currently returned by Delta's
    # private OPEN-ORDERS endpoint for this OWNER account.
    # Bot orders and manually placed orders are both shown.
    _chart_grid_lines = []
    _chart_open_order_labels = []
    _chart_target_lines = []

    if grid_open_ok:
        for order in grid_open_orders:
            _p = _grid_price(order)
            if _p is None:
                continue
            _state = str(order.get("state", "")).lower()
            if _state in {"cancelled", "filled", "rejected"}:
                continue

            _cid = str(order.get("client_order_id", "")).strip()
            _role, _level_no = _grid_client_role_level(order)
            # FINAL CHART COLOR RULE:
            # ENTRY / RE-ENTRY = GREEN, TARGET = RED.
            # This is visual only; it never changes order side or trading logic.
            if _role == "TARGET":
                _line_role = "TARGET"
                _line_color = "#ef4444"
            elif _role == "ENTRY":
                _line_role = "PENDING ENTRY"
                _line_color = "#22c55e"
            elif _role == "REENTRY":
                _line_role = "PENDING REENTRY"
                _line_color = "#22c55e"
            else:
                # Manual / externally-created order: show it exactly as Delta
                # reports it; never infer a target or grid level.
                _line_role = "OPEN ORDER"
                _line_color = "#60a5fa"

            _q = _grid_contracts(order)
            _side = str(order.get("side", "")).upper()
            _order_id = str(order.get("id", order.get("order_id", ""))).strip()
            _level_text = f"L-{_level_no}" if _level_no else ""
            _chart_grid_lines.append({
                "price": float(_p),
                "color": _line_color,
                "width": 3,
                "dash": "solid",
                "label": (
                    f"{_line_role} {_level_text} • {show_price(_p)} • "
                    f"{_q * CONTRACT_XRP:.3f} XRP • {_side} • ORDER {_order_id}"
                ),
                "role": _line_role,
                "order_id": _order_id,
                "client_id": _cid,
            })
            _chart_open_order_labels.append(
                f"{_line_role} {_level_text} {_side} "
                f"{_q * CONTRACT_XRP:.3f} XRP @ {show_price(_p)} • ORDER {_order_id}"
            )

    # ------------------------------------------------------------
    # IMPORTANT: DO NOT DRAW PLANNED/FUTURE GRID LINES
    # ------------------------------------------------------------
    # A line is drawn on the chart ONLY when the corresponding order is
    # actually open on Delta right now.  Fixed grid levels that are merely
    # calculated/armed, rejected for margin, or not yet submitted are NOT
    # displayed as order lines.  This prevents red/green lines from appearing
    # before a real exchange order exists.

    # Historical SuperTrend signals.
    _chart_signal_x = []
    _chart_signal_y = []
    _chart_signal_text = []
    _chart_signal_color = []
    # ONLY TRUE SuperTrend direction-change candles get a marker.
    # Entry/re-entry/target orders NEVER create markers or dots.
    for _, row in signal_rows.drop_duplicates(subset=["time"]).tail(1).iterrows():
        _sig = str(row.get("SIGNAL", "")).upper().strip()
        if _sig not in {"BUY", "SELL"}:
            continue
        try:
            _signal_ts = int(float(row["time"]))
            _signal_close = float(row["close"])
        except Exception:
            continue
        _chart_signal_x.append(
            datetime.fromtimestamp(_signal_ts, tz=timezone.utc)
            .astimezone(IST).strftime("%Y-%m-%d %H:%M")
        )
        _chart_signal_y.append(_signal_close)
        _chart_signal_text.append(_sig)
        _chart_signal_color.append("#22c55e" if _sig == "BUY" else "#ff3b30")

    # ============================================================
    # CHART Y-AXIS = ALL REAL ACTIVE ORDER PRICES
    # ============================================================
    # Candles remain the base layer.  Every REAL active ENTRY/RE-ENTRY
    # and TARGET order must remain visible on the SAME price chart.
    # Therefore the Y-axis is calculated from ALL candle prices + ALL
    # real active order prices.  If orders are far above/below candles,
    # the candles may become smaller — that is intentional.  We NEVER
    # hide, clip, offset, or move a real order line just to keep candles large.
    # The live ticker itself is not used to continuously resize the viewport.
    _chart_candle_values = _chart_high + _chart_low + _chart_close
    _chart_order_values = [
        float(L["price"]) for L in _chart_grid_lines
        if L.get("price") is not None
    ]
    _chart_values_for_view = _chart_candle_values + _chart_order_values
    if not _chart_values_for_view:
        _chart_values_for_view = [0.0, 1.0]

    _chart_view_data_min = min(_chart_values_for_view)
    _chart_view_data_max = max(_chart_values_for_view)
    _chart_view_span = max(_chart_view_data_max - _chart_view_data_min, 1.0)
    _chart_pad = max(_chart_view_span * 0.05, 15.0)

    # Recalculate the viewport whenever the completed candles or REAL
    # active order set changes.  A normal live-price refresh does not move it.
    _chart_view_key = (
        tuple(_chart_times),
        tuple(
            (str(x.get("order_id", "")), round(float(x["price"]), 2), str(x.get("role", "")))
            for x in _chart_grid_lines
        ),
        grid_direction or "-"
    )
    if st.session_state.get("chart_view_key") != _chart_view_key:
        st.session_state["chart_view_key"] = _chart_view_key
        st.session_state["chart_view_min"] = _chart_view_data_min - _chart_pad
        st.session_state["chart_view_max"] = _chart_view_data_max + _chart_pad

    _chart_min = float(
        st.session_state.get("chart_view_min", _chart_view_data_min - _chart_pad)
    )
    _chart_max = float(
        st.session_state.get("chart_view_max", _chart_view_data_max + _chart_pad)
    )

    import json as _json

    # Additive chart badge data: reuse the position already fetched by the grid engine.
    _chart_entry_price = number(
        grid_position.get("entry_price", grid_position.get("average_entry_price", grid_position.get("avg_entry_price")))
    ) if isinstance(grid_position, dict) else None
    _chart_position_size = number(grid_position.get("size"), 0.0) if isinstance(grid_position, dict) else 0.0

    _chart_payload = {
        "times": _chart_times,
        "open": _chart_open,
        "high": _chart_high,
        "low": _chart_low,
        "close": _chart_close,
        "tsl_price": _chart_tsl_price,
        "tsl_color": _chart_tsl_color,
        "signals_x": _chart_signal_x,
        "signals_y": _chart_signal_y,
        "signals_text": _chart_signal_text,
        "signals_color": _chart_signal_color,
        "cycle_signal_x": _chart_signal_x[-1] if _chart_signal_x else (_chart_times[0] if _chart_times else ""),
        "last_candle_x": _chart_times[-1] if _chart_times else "",
        "grid_lines": _chart_grid_lines,
        "target_lines": _chart_target_lines,
        "open_orders": _chart_open_order_labels,
        "direction": grid_direction or "-",
        "symbol": SYMBOL,
        "timeframe": "1M",
        "min_price": _chart_min - _chart_pad,
        "max_price": _chart_max + _chart_pad,
        # Live ticker price is intentionally separate from the completed-candle
        # close used by the SuperTrend engine. It never changes fixed levels or targets.
        "current_price": float(live_price),
    }

    _chart_json = _json.dumps(_chart_payload, ensure_ascii=False)

    components.html(
        """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Delta Exchange Chart</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  body { background:#131722; color:#d1d4dc; font-family:Arial,sans-serif; margin:0; padding:10px; overflow:hidden; }
  #toolbar { margin-bottom:10px; white-space:nowrap; overflow-x:auto; }
  .tf-btn {
    background:#2a2e39; color:#d1d4dc; border:1px solid #434651;
    padding:6px 14px; margin-right:5px; cursor:pointer; border-radius:4px;
  }
  .tf-btn.active { background:#2962ff; color:#fff; }
  #chart-wrapper { position:relative; width:100%; }
  #chart { width:100%; height:1200px; }
  #status { margin-top:8px; font-size:12px; color:#787b86; }
  #live-pnl-badge { display:none; position:absolute; left:8px; z-index:5;
    pointer-events:none; font-family:Arial,sans-serif; }
  #live-pnl-qty { display:inline-block; min-width:22px; text-align:center;
    padding:4px 8px; border-radius:14px; font-weight:900; font-size:12px;
    background:#2962ff; color:#fff; box-shadow:0 2px 6px rgba(0,0,0,.4); }
  #live-pnl-value { display:inline-block; margin-left:4px; padding:4px 10px;
    border-radius:14px; font-weight:900; font-size:13px;
    box-shadow:0 2px 6px rgba(0,0,0,.4); color:#fff; }
  #live-price-box {
    display:inline-flex; align-items:center; gap:8px; margin-left:14px;
    padding:6px 11px; border-radius:7px; border:1px solid #3a3f4b;
    background:#171a21; font-size:15px; font-weight:900;
    vertical-align:middle;
  }
  #live-price-label { color:#9aa4b2; font-size:11px; letter-spacing:.4px; }
  #live-price-value { color:#d1d4dc; font-variant-numeric:tabular-nums; }
</style>
</head>
<body>

<div id="toolbar">
  <span class="tf-btn active" aria-label="One-minute display chart">1M</span>
  <span id="live-price-box"><span id="live-price-label">LIVE PRICE</span><span id="live-price-value">--</span></span>
  <button class="tf-btn" id="fullscreen-btn" type="button">⛶ Fullscreen</button>
</div>

<div id="chart-wrapper">
  <div id="chart"></div>
  <div id="live-pnl-badge">
    <span id="live-pnl-qty">--</span>
    <span id="live-pnl-value">--</span>
  </div>
</div>
<div id="status">Connecting...</div>

<script>
const SERVER_DATA = __CHART_DATA__;
const SYMBOL = "__SYMBOL__";
const BASE_URL = "__PUBLIC_BASE_URL__";
const WS_URL = "__WS_URL__";
const POSITION_ENTRY_PRICE = __POSITION_ENTRY_PRICE__;
const POSITION_SIZE = __POSITION_SIZE__;
const CONTRACT_XRP_VALUE = __CONTRACT_XRP_VALUE__;

const ATR_PERIOD = 10;
const ATR_MULTIPLIER = 2;

let currentResolution = "1m";
let candles = [];
let ws = null;
let reconnectTimer = null;
let pingTimer = null;

const chartContainer = document.getElementById('chart');
const statusEl = document.getElementById('status');

const chart = LightweightCharts.createChart(chartContainer, {
  layout: { background: { color: '#131722' }, textColor: '#d1d4dc' },
  grid: { vertLines: { color: '#1e222d' }, horzLines: { color: '#1e222d' } },
  timeScale: { borderColor: '#434651', timeVisible: true, secondsVisible: false, rightOffset: 5 },
  rightPriceScale: { borderColor: '#434651' }
});

const candleSeries = chart.addCandlestickSeries({
  upColor: '#26a69a',
  downColor: '#ef5350',
  borderVisible: false,
  wickUpColor: '#26a69a',
  wickDownColor: '#ef5350',
  priceLineVisible: false,
  lastValueVisible: true,
  priceFormat: { type: 'price', precision: 6, minMove: 0.000001 }
});

// SuperTrend is drawn as separate color segments.
// IMPORTANT: the flip candle belongs to ONLY the new color segment.
// This prevents the old/new series from overlapping at the direction change.
let supertrendSegments = [];
let selectedIndicator = "none"; // Chart displays real market candles/orders only; no calculated SuperTrend line.

let orderPriceLines = [];
let livePriceLine = null;
let previousLivePrice = null;

const livePriceValueEl = document.getElementById('live-price-value');

function formatLivePrice(price) {
  if (!Number.isFinite(Number(price))) return '--';
  return Number(price).toLocaleString('en-US', {
    minimumFractionDigits: 6,
    maximumFractionDigits: 6
  });
}

function liveMovementColor(price) {
  const p = Number(price);
  if (!Number.isFinite(p) || previousLivePrice === null) return '#d1d4dc';
  if (p > previousLivePrice) return '#22c55e';
  if (p < previousLivePrice) return '#ef5350';
  return '#d1d4dc';
}

function updateLivePrice(price, remember = true) {
  const p = Number(price);
  if (!Number.isFinite(p)) return;

  // Additive live QTY + approximate PNL overlay; display-only.
  const badge = document.getElementById('live-pnl-badge');
  const qtyEl = document.getElementById('live-pnl-qty');
  const pnlEl = document.getElementById('live-pnl-value');
  if (badge && qtyEl && pnlEl) {
    if (!POSITION_SIZE || POSITION_ENTRY_PRICE === null ||
        POSITION_ENTRY_PRICE === undefined ||
        !Number.isFinite(Number(POSITION_ENTRY_PRICE))) {
      badge.style.display = 'none';
    } else {
      const pnlValue = (p - Number(POSITION_ENTRY_PRICE)) *
                       Number(POSITION_SIZE) * Number(CONTRACT_XRP_VALUE);
      const isProfit = pnlValue >= 0;
      qtyEl.textContent = Math.abs(Number(POSITION_SIZE)).toFixed(0);
      pnlEl.textContent = (isProfit ? '+' : '-') + Math.abs(pnlValue).toFixed(4) + ' USD';
      pnlEl.style.background = isProfit ? '#16a34a' : '#ef4444';
      pnlEl.style.color = '#ffffff';
      const yCoord = candleSeries.priceToCoordinate(p);
      if (yCoord !== null && yCoord !== undefined) badge.style.top = (yCoord - 11) + 'px';
      badge.style.display = 'block';
    }
  }

  const color = liveMovementColor(p);
  if (livePriceValueEl) {
    livePriceValueEl.textContent = formatLivePrice(p);
    livePriceValueEl.style.color = color;
  }

  if (livePriceLine) {
    try {
      livePriceLine.applyOptions({ price: p, color });
    } catch (e) {}
  }

  if (remember) previousLivePrice = p;
}

window.addEventListener('resize', () => {
  chart.resize(chartContainer.clientWidth, chartContainer.clientHeight);
});

document.getElementById('fullscreen-btn').addEventListener('click', async () => {
  const wrapper = document.getElementById('chart-wrapper');
  try {
    if (!document.fullscreenElement) {
      await wrapper.requestFullscreen();
    } else {
      await document.exitFullscreen();
    }
  } catch (e) {}
});
document.addEventListener('fullscreenchange', () => {
  chart.resize(chartContainer.clientWidth, chartContainer.clientHeight);
});

function calculateSupertrend(data, period, multiplier) {
  const result = [];
  let atr = 0;
  const finalUpperBand = [];
  const finalLowerBand = [];
  const trend = [];

  for (let i = 0; i < data.length; i++) {
    const curr = data[i];
    const prev = data[i - 1];

    let tr;
    if (i === 0) {
      tr = curr.high - curr.low;
    } else {
      tr = Math.max(
        curr.high - curr.low,
        Math.abs(curr.high - prev.close),
        Math.abs(curr.low - prev.close)
      );
    }

    if (i === 0) {
      atr = tr;
    } else if (i < period) {
      atr = (atr * i + tr) / (i + 1);
    } else {
      atr = (atr * (period - 1) + tr) / period;
    }

    const mid = (curr.high + curr.low) / 2;
    const basicUpper = mid + multiplier * atr;
    const basicLower = mid - multiplier * atr;

    let fUpper, fLower;
    if (i === 0) {
      fUpper = basicUpper;
      fLower = basicLower;
    } else {
      fUpper =
        (basicUpper < finalUpperBand[i - 1] ||
         data[i - 1].close > finalUpperBand[i - 1])
          ? basicUpper
          : finalUpperBand[i - 1];

      fLower =
        (basicLower > finalLowerBand[i - 1] ||
         data[i - 1].close < finalLowerBand[i - 1])
          ? basicLower
          : finalLowerBand[i - 1];
    }

    finalUpperBand[i] = fUpper;
    finalLowerBand[i] = fLower;

    let dir;
    if (i === 0) {
      dir = 1;
    } else if (trend[i - 1] === 1 && curr.close < fLower) {
      dir = -1;
    } else if (trend[i - 1] === -1 && curr.close > fUpper) {
      dir = 1;
    } else {
      dir = trend[i - 1];
    }

    trend[i] = dir;
    result.push({
      time: curr.time,
      value: dir === 1 ? fLower : fUpper,
      trend: dir
    });
  }

  return result;
}

function renderSupertrend(stData) {
  // Clean segmented SuperTrend drawing:
  // BUY = green, SELL = red. When the direction changes, the previous
  // segment is closed and a new segment starts at the flip candle.
  // The flip candle is NEVER drawn in both series, so there is no double line.

  for (const series of supertrendSegments) {
    try { chart.removeSeries(series); } catch (e) {}
  }
  supertrendSegments = [];

  if (!Array.isArray(stData) || stData.length === 0) return;

  let currentSeries = null;
  let currentColor = null;
  let currentData = [];

  for (let i = 0; i < stData.length; i++) {
    const item = stData[i];
    if (!item || !Number.isFinite(Number(item.value))) continue;

    const val = Number(item.value);
    // Existing engine convention: trend === -1 = BUY, trend === 1 = SELL.
    const col = Number(item.trend) === -1 ? '#22c55e' : '#ef5350';

    if (currentColor !== col) {
      if (currentSeries && currentData.length > 0) {
        currentSeries.setData(currentData);
      }

      currentColor = col;
      currentData = [];
      currentSeries = chart.addLineSeries({
        color: currentColor,
        lineWidth: 3,
        priceLineVisible: false,
        lastValueVisible: true,
        crosshairMarkerVisible: false,
        priceFormat: { type: 'price', precision: 6, minMove: 0.000001 }
      });
      supertrendSegments.push(currentSeries);
    }

    currentData.push({
      time: item.time,
      value: val
    });
  }

  if (currentSeries && currentData.length > 0) {
    currentSeries.setData(currentData);
  }
}

function clearOrderLines() {
  for (const line of orderPriceLines) {
    try { candleSeries.removePriceLine(line); } catch (e) {}
  }
  orderPriceLines = [];

  if (livePriceLine) {
    try { candleSeries.removePriceLine(livePriceLine); } catch (e) {}
    livePriceLine = null;
  }
}

function drawServerOrderLines() {
  clearOrderLines();

  const lines = Array.isArray(SERVER_DATA.grid_lines)
    ? SERVER_DATA.grid_lines
    : [];

  for (const L of lines) {
    const price = Number(L.price);
    if (!Number.isFinite(price)) continue;

    const role = String(L.role || '').toUpperCase();
    const color = L.color || (
      role.includes('TARGET') ? '#ef4444' :
      role.includes('ENTRY') || role.includes('REENTRY') ? '#22c55e' :
      '#60a5fa'
    );
    const style = String(L.dash || '').toLowerCase() === 'dashed'
      ? LightweightCharts.LineStyle.Dashed
      : LightweightCharts.LineStyle.Solid;

    orderPriceLines.push(
      candleSeries.createPriceLine({
        price,
        color,
        lineWidth: Number(L.width || 2),
        lineStyle: style,
        axisLabelVisible: true,
        title: role.includes('TARGET') ? 'TARGET' : 'ENTRY'
      })
    );
  }

  const live = Number(SERVER_DATA.current_price);
  if (Number.isFinite(live)) {
    livePriceLine = candleSeries.createPriceLine({
      price: live,
      color: '#d1d4dc',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true,
      title: 'LIVE'
    });
    previousLivePrice = null;
    updateLivePrice(live, true);
  }
}

function drawServerSignalMarkers() {
  const markers = [];
  const xs = Array.isArray(SERVER_DATA.signals_x) ? SERVER_DATA.signals_x : [];
  const ys = Array.isArray(SERVER_DATA.signals_y) ? SERVER_DATA.signals_y : [];
  const texts = Array.isArray(SERVER_DATA.signals_text) ? SERVER_DATA.signals_text : [];

  for (let i = 0; i < xs.length; i++) {
    const t = Math.floor(Date.parse(String(xs[i]).replace(' ', 'T') + ':00+05:30') / 1000);
    if (!Number.isFinite(t)) continue;

    const text = String(texts[i] || '').toUpperCase();
    const buy = text.includes('BUY');
    const entry = ys[i] != null ? Number(ys[i]) : null;

    markers.push({
      time: t,
      position: buy ? 'belowBar' : 'aboveBar',
      color: buy ? '#22c55e' : '#ef5350',
      shape: buy ? 'arrowUp' : 'arrowDown',
      text: Number.isFinite(entry)
        ? ((buy ? 'BUY ' : 'SELL ') +
           entry.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}))
        : text
    });
  }

  markers.sort((a, b) => a.time - b.time);
  if (markers.length) candleSeries.setMarkers(markers);
}

async function fetchCandles(resolution) {
  const end = Math.floor(Date.now() / 1000);
  const barsNeeded = 300;
  const resSeconds = {"1m":60};
  const start = end - (resSeconds[resolution] * barsNeeded);

  const url =
    BASE_URL + "/history/candles?symbol=" +
    encodeURIComponent(SYMBOL) +
    "&resolution=" + encodeURIComponent(resolution) +
    "&start=" + start +
    "&end=" + end;

  const res = await fetch(url);
  const json = await res.json();

  if (!json.success) {
    throw new Error(JSON.stringify(json));
  }

  return (json.result || [])
    .sort((a, b) => Number(a.time) - Number(b.time))
    .map(c => ({
      time: Number(c.time),
      open: Number(c.open),
      high: Number(c.high),
      low: Number(c.low),
      close: Number(c.close),
      volume: Number(c.volume || 0)
    }));
}

async function loadChart(resolution) {
  statusEl.innerText = "Loading " + resolution + " candles...";
  currentResolution = resolution;

  try {
    candles = await fetchCandles(resolution);

    candleSeries.setData(candles.map(c => ({
      time: c.time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close
    })));

    // SuperTrend is confirmed-candle only; the current live candle is not
    // allowed to create or visually confirm an intra-candle signal.
    const initialConfirmedCandles = candles.length > 1
      ? candles.slice(0, -1)
      : candles;

    // Chart-only display rule: do not draw the calculated SuperTrend line
    // or indicator-derived signal markers. Keep actual exchange order/target
    // lines, real OHLC candles, and the live exchange price.
    renderSupertrend([]);

    drawServerOrderLines();

    chart.timeScale().fitContent();
    statusEl.innerText =
      "Live: " + SYMBOL + " (" + resolution + ")";

    subscribeWebSocket(resolution);
  } catch (err) {
    statusEl.innerText = "Error fetching candles: " + err.message;
    console.error(err);
  }
}

function subscribeWebSocket(resolution) {
  if (ws) {
    try { ws.close(); } catch (e) {}
    ws = null;
  }

  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  if (pingTimer) {
    clearInterval(pingTimer);
    pingTimer = null;
  }

  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    ws.send(JSON.stringify({
      type: "subscribe",
      payload: {
        channels: [
          { name: "candlestick_" + resolution, symbols: [SYMBOL] }
        ]
      }
    }));

    pingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ping" }));
      }
    }, 25000);
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);

      if (!msg.type || !msg.type.startsWith("candlestick_")) return;

      const resSeconds = {"1m":60};

      let candleTime;

      if (msg.ts != null) {
        const rawTs = Number(msg.ts);
        candleTime = rawTs > 100000000000
          ? Math.floor(rawTs / 1000000)
          : Math.floor(rawTs);
      } else {
        candleTime = Math.floor(Date.now() / 1000);
      }

      const bucketTime =
        Math.floor(candleTime / resSeconds[currentResolution]) *
        resSeconds[currentResolution];

      const newCandle = {
        time: bucketTime,
        open: Number(msg.o),
        high: Number(msg.h),
        low: Number(msg.l),
        close: Number(msg.c),
        volume: Number(msg.v || 0)
      };

      if (
        !Number.isFinite(newCandle.open) ||
        !Number.isFinite(newCandle.high) ||
        !Number.isFinite(newCandle.low) ||
        !Number.isFinite(newCandle.close)
      ) return;

      const last = candles[candles.length - 1];

      // REAL LIVE CANDLE RULE:
      // - Open is fixed by the first real exchange value for this timestamp.
      // - Every later tick can only move High upward, Low downward, and Close
      //   to the latest real market value.
      // - The same timestamp is updated in-place, so duplicate candles are
      //   never created.
      if (last && last.time === newCandle.time) {
        newCandle.open = Number(last.open);
        newCandle.high = Math.max(Number(last.high), newCandle.high, newCandle.open);
        newCandle.low = Math.min(Number(last.low), newCandle.low, newCandle.open);
        candles[candles.length - 1] = newCandle;
      } else if (!last || newCandle.time > last.time) {
        newCandle.high = Math.max(newCandle.high, newCandle.open);
        newCandle.low = Math.min(newCandle.low, newCandle.open);
        candles.push(newCandle);
        if (candles.length > 350) candles.shift();
      } else {
        return;
      }

      candleSeries.update({
        time: newCandle.time,
        open: newCandle.open,
        high: newCandle.high,
        low: newCandle.low,
        close: newCandle.close
      });

      // The latest real candle close/tick is the live market price.
      // This is display-only and never changes SuperTrend/order logic.
      updateLivePrice(newCandle.close, true);

      // No calculated SuperTrend line or indicator-derived markers on chart.
      // Signal logic elsewhere in the dashboard is intentionally unchanged.
      renderSupertrend([]);
    } catch (e) {
      console.error("WebSocket candle error", e);
    }
  };

  ws.onerror = (err) => {
    statusEl.innerText = "WebSocket error";
    console.error(err);
  };

  ws.onclose = () => {
    if (pingTimer) {
      clearInterval(pingTimer);
      pingTimer = null;
    }

    statusEl.innerText = "Disconnected. Reconnecting...";

    reconnectTimer = setTimeout(() => {
      subscribeWebSocket(currentResolution);
    }, 3000);
  };
}

// DISPLAY ONLY: one-minute candles. Trading/SuperTrend engine remains on its original fixed 1H data.
loadChart("1m");
</script>

</body>
</html>
"""
        .replace("__CHART_DATA__", _chart_json)
        .replace("__SYMBOL__", str(SYMBOL).replace("\\", "\\\\").replace('"', '\\"'))
        .replace("__TIMEFRAME__", "1m")
        .replace("__PUBLIC_BASE_URL__", PUBLIC_REST_V2_URL)
        .replace("__WS_URL__", PUBLIC_WS_URL)
        .replace("__POSITION_ENTRY_PRICE__", _json.dumps(_chart_entry_price))
        .replace("__POSITION_SIZE__", _json.dumps(_chart_position_size or 0.0))
        .replace("__CONTRACT_XRP_VALUE__", _json.dumps(CONTRACT_XRP)),
        height=1200,
        scrolling=False
    )

    # ============================================================
    # CHART-BELOW PRICE STRIP — DISPLAY ONLY
    # Uses exchange-reported liquidation price and the existing fixed L13.
    # Missing exchange values are shown as unavailable; no estimates are used.
    # ============================================================
    _strip_liquidation_price = None
    _strip_liquidation_status = "EXCHANGE DATA UNAVAILABLE"
    try:
        _strip_margin_response = api.margined_positions()
        _strip_margin_rows = _grid_result(_strip_margin_response)
        if isinstance(_strip_margin_rows, dict):
            _strip_margin_rows = [_strip_margin_rows]
        if isinstance(_strip_margin_rows, list):
            _strip_position = next(
                (
                    _r for _r in _strip_margin_rows
                    if isinstance(_r, dict)
                    and int(float(_r.get("size", 0) or 0)) != 0
                ),
                None,
            )
            if _strip_position:
                _strip_liq_raw = _strip_position.get("liquidation_price")
                if _strip_liq_raw not in (None, ""):
                    _strip_liquidation_price = float(_strip_liq_raw)
                    _strip_liquidation_status = "LIVE EXCHANGE LIQUIDATION PRICE"
                else:
                    _strip_liquidation_status = "LIQUIDATION PRICE NOT RETURNED BY EXCHANGE"
            else:
                _strip_liquidation_status = "NO OPEN POSITION — NO LIQUIDATION PRICE"
    except Exception:
        _strip_liquidation_status = "EXCHANGE DATA UNAVAILABLE"

    # Use the live ticker value already retrieved from Delta's public API.
    _strip_live_price = number(live_price)
    _strip_l13_price = (
        number(grid_levels[-1]) if isinstance(grid_levels, list) and len(grid_levels) >= 13
        else None
    )
    _strip_liq_text = show_price(_strip_liquidation_price) if _strip_liquidation_price is not None else "UNAVAILABLE"
    _strip_live_text = show_price(_strip_live_price) if _strip_live_price is not None else "UNAVAILABLE"
    _strip_l13_text = show_price(_strip_l13_price) if _strip_l13_price is not None else "UNAVAILABLE"
    _strip_liq_side = "LONG" if _strip_liquidation_price is not None and _strip_live_price is not None and _strip_liquidation_price < _strip_live_price else "SHORT / UNKNOWN"

    st.markdown(
        f"""
        <div style="width:100%;box-sizing:border-box;margin:14px 0 18px;padding:10px 8px 8px;
                    font-family:Arial,sans-serif;color:#111827;">
          <div style="display:flex;align-items:stretch;gap:0;width:100%;min-height:54px;">
            <div style="flex:1.1;min-width:0;background:#ef4444;border-radius:18px 0 0 18px;
                        padding:9px 5px;text-align:center;color:white;overflow-wrap:anywhere;">
              <div style="font-size:clamp(11px,2.5vw,16px);font-weight:900;">⚠ LIQUIDATION PRICE</div>
              <div style="font-size:clamp(15px,3.3vw,21px);font-weight:900;">{_strip_liq_text}</div>
            </div>
            <div style="flex:1;min-width:0;background:#f8fafc;border-top:3px solid #64748b;
                        border-bottom:3px solid #64748b;padding:9px 5px;text-align:center;
                        overflow-wrap:anywhere;">
              <div style="font-size:clamp(11px,2.5vw,16px);font-weight:900;color:#111827;">● LIVE PRICE</div>
              <div style="font-size:clamp(15px,3.3vw,21px);font-weight:900;color:#111827;">{_strip_live_text}</div>
            </div>
            <div style="flex:1.1;min-width:0;background:#22c55e;border-radius:0 18px 18px 0;
                        padding:9px 5px;text-align:center;color:#052e16;overflow-wrap:anywhere;">
              <div style="font-size:clamp(11px,2.5vw,16px);font-weight:900;">▲ L-13 PRICE</div>
              <div style="font-size:clamp(15px,3.3vw,21px);font-weight:900;">{_strip_l13_text}</div>
            </div>
          </div>
          <div style="display:flex;justify-content:space-between;gap:8px;margin-top:5px;
                      font-size:clamp(10px,2.5vw,13px);font-weight:700;text-align:center;">
            <div style="flex:1;color:#b91c1c;">{_strip_liquidation_status}</div>
            <div style="flex:1;color:#334155;">LIVE DELTA TICKER</div>
            <div style="flex:1;color:#15803d;">L13 — FIXED ATR GRID</div>
          </div>
          <div style="font-size:11px;color:#64748b;text-align:center;margin-top:7px;">
            केवल Delta Exchange से मिला liquidation price दिखाया जाता है; कोई अनुमानित price नहीं।
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


    # ============================================================
    # ============================================================



    # ============================================================
# ============================================================
# RUN OWNER ACCOUNT ONLY ON EVERY REFRESH
_owner_key, _owner_secret = _account_credentials("OWNER ACCOUNT")

if _owner_key and _owner_secret:
    _run_account_engine(
        "OWNER ACCOUNT",
        _account_client("OWNER ACCOUNT"),
        OWNER_GRID_QTY,
        True,
    )
else:
    st.error(
        "❌ Owner API credentials नहीं मिले। REAL_OWNER_API_KEY और "
        "REAL_OWNER_API_SECRET को उसी hosting environment में सेट करें जहाँ Dashboard चलता है।"
    )
# END — OWNER ACCOUNT ONLY / ORIGINAL GRID LOGIC PRESERVED
# ============================================================

time.sleep(max(3, int(REFRESH_SECONDS)))
st.rerun()
