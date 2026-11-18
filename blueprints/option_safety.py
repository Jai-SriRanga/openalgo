"""
Option Safety Check Blueprint
Pre-trade check: will this option pay if the underlying moves as expected,
after IV crush and theta?
"""

from flask import Blueprint, jsonify, request, session
from flask_cors import cross_origin

from database.auth_db import get_api_key_for_tradingview, get_auth_token
from services.option_safety_service import get_option_safety_check
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

option_safety_bp = Blueprint("option_safety_bp", __name__, url_prefix="/")


def _float_or_none(value):
    if value is None or value == "":
        return None
    return float(value)


@option_safety_bp.route("/optionsafety/api/check", methods=["POST"])
@cross_origin()
@check_session_validity
def safety_check():
    """Run the pre-trade option safety check."""
    try:
        if not session.get("broker"):
            return jsonify({"status": "error", "message": "Broker not set in session"}), 400

        login_username = session["user"]
        if get_auth_token(login_username) is None:
            return jsonify({"status": "error", "message": "Authentication required"}), 401

        api_key = get_api_key_for_tradingview(login_username)
        if not api_key:
            return jsonify(
                {
                    "status": "error",
                    "message": "API key not configured. Please generate an API key in /apikey",
                }
            ), 401

        data = request.get_json(silent=True) or {}
        underlying = str(data.get("underlying", "")).strip()
        exchange = str(data.get("exchange", "")).strip()
        expiry_date = str(data.get("expiry_date", "")).strip()
        if not underlying or not exchange or not expiry_date:
            return jsonify(
                {"status": "error", "message": "underlying, exchange and expiry_date are required"}
            ), 400

        try:
            target_move_pct = float(data.get("target_move_pct", 0))
            holding_days = float(data.get("holding_days", 1))
            strike = _float_or_none(data.get("strike"))
            iv_after_override = _float_or_none(data.get("iv_after_override"))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Numeric fields must be numbers"}), 400

        if not -50 <= target_move_pct <= 50:
            return jsonify(
                {"status": "error", "message": "target_move_pct must be between -50 and 50"}
            ), 400
        if not 0 <= holding_days <= 365:
            return jsonify(
                {"status": "error", "message": "holding_days must be between 0 and 365"}
            ), 400

        success, response, status_code = get_option_safety_check(
            underlying=underlying,
            exchange=exchange,
            expiry_date=expiry_date,
            option_type=str(data.get("option_type", "CE")),
            side=str(data.get("side", "BUY")),
            target_move_pct=target_move_pct,
            holding_days=holding_days,
            strike=strike,
            next_expiry_date=(str(data.get("next_expiry_date") or "").strip() or None),
            event_expected=bool(data.get("event_expected", False)),
            iv_after_override=iv_after_override,
            api_key=api_key,
        )
        return jsonify(response), status_code

    except Exception as e:
        logger.exception(f"Error in option safety check API: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
