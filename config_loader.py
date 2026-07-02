"""config.yaml のロードとデフォルト値マージを行うモジュール。"""

import copy
import os
import sys
from typing import Any

import yaml


def get_user_config_path() -> str:
    """GUI から書き込む config.yaml のパス（exe 横 or スクリプト横）。"""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "config.yaml")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")


def _get_config_path() -> str:
    if getattr(sys, "frozen", False):
        # exe 横のユーザー設定ファイルを優先し、なければバンドル版を使う
        user = get_user_config_path()
        if os.path.exists(user):
            return user
        return os.path.join(sys._MEIPASS, "config.yaml")  # type: ignore[attr-defined]
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

_DEFAULTS: dict[str, Any] = {
    "input_base_dir": "",
    "output_dir": "",
    "product_names": {},
    "products": {},
    "default_thresholds": {
        "mean_diff_warn_g": 0.2,
        "mean_diff_alert_g": 0.5,
        "sigma_warn_g": 0.4,
        "sigma_alert_g": 0.5,
        "light_ng_rate_warn": 0.002,
        "light_ng_rate_alert": 0.005,
        "heavy_ng_rate_warn": 0.002,
        "heavy_ng_rate_alert": 0.005,
        "twostack_rate_warn": 0.001,
        "twostack_rate_alert": 0.003,
        "device_abnormal_rate_warn": 0.001,
        "device_abnormal_rate_alert": 0.003,
    },
    "ok_rank_codes": ["2"],
    "ok_pass_senbetsu": ["1"],
    "thresholds": {
        "defect_rate_multiple": 3.0,
        "mean_sigma_multiple": 2.0,
        "std_multiple": 1.5,
        "min_data_count": 1000,
        "double_ride_rate": 0.5,
        "device_abnormal_rate": 0.5,
    },
    "all_data_chart": {
        "enabled": True,
        "lot_gap_threshold_seconds": 1800,
        "y_axis_mode": "fixed",
        "y_axis_sigma_range": 5,
        "reference_lines": {
            "show_mean": True,
            "show_2sigma": True,
            "show_3sigma": True,
            "show_reject_limit": False,
            "reject_limit_g": 1.2,
        },
    },
    "ok_only_chart": {
        "show_moving_average": True,
        "moving_average_window": 1000,
    },
    "daily_summary": {
        "enabled": True,
        "output_format": "per_day",
        "output_directory": "./DailySummary",
        "lot_gap_threshold_seconds": 1800,
        "min_records_per_lot": 50,
        "baseline_period_days": 30,
        "exclude_abnormal_days": True,
        "reject_limit_g": 1.2,
        "thresholds": {
            "mean_diff_warn_g": 0.2,
            "mean_diff_alert_g": 0.5,
            "sigma_ratio_warn": 1.2,
            "sigma_ratio_alert": 1.5,
            "ng_rate_ratio_warn": 2.0,
            "ng_rate_ratio_alert": 5.0,
            "twostack_rate_warn": 0.001,
            "twostack_rate_alert": 0.003,
            "device_abnormal_rate_warn": 0.001,
            "device_abnormal_rate_alert": 0.003,
        },
        "product_thresholds": {},
    },
    "recommended_actions": {
        "軽量NG率_異常": [
            "軽量NG発生時刻のクラスター確認",
            "部材ロット切替の有無を製造記録で確認",
            "充填システム設定の確認",
        ],
        "過量NG率_異常": [
            "過量NG発生時刻のクラスター確認",
            "充填システム設定の確認",
        ],
        "平均下振れ_異常": [
            "基準値設定時の最初5個と実平均を比較",
            "基準値妥当性の確認",
            "充填システムのセンタリング確認",
        ],
        "平均上振れ_異常": [
            "基準値設定時の最初5個と実平均を比較",
            "充填システムのセンタリング確認",
        ],
        "シグマ増大_異常": [
            "ロット内の重量推移を時系列確認",
            "ドリフト・ばらつき要因の調査",
        ],
        "二個乗り_異常": [
            "二個乗り発生時刻の確認",
            "搬送・供給状態の確認",
        ],
        "装置異常_異常": [
            "装置動作異常レコードの時刻・種別確認",
            "設備保全への連絡検討",
        ],
    },
}

_cached: dict[str, Any] | None = None

# yaml のネストされたキーはデフォルト値と dict.update でマージする
_DEEP_MERGE_KEYS = frozenset(
    {"thresholds", "all_data_chart", "ok_only_chart", "daily_summary", "default_thresholds"}
)


def load_config(path: str | None = None, *, force_reload: bool = False) -> dict[str, Any]:
    """config.yaml を読み込んでデフォルト値とマージした辞書を返す。"""
    global _cached
    if _cached is not None and not force_reload:
        return _cached
    if path is None:
        path = _get_config_path()

    config: dict[str, Any] = copy.deepcopy(_DEFAULTS)

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user: dict[str, Any] = yaml.safe_load(f) or {}
        for key, val in user.items():
            if val is None:
                continue
            if key in _DEEP_MERGE_KEYS and isinstance(val, dict):
                if key == "all_data_chart" and isinstance(val.get("reference_lines"), dict):
                    config[key] = copy.deepcopy(_DEFAULTS[key])
                    val_copy = dict(val)
                    rl = val_copy.pop("reference_lines", {})
                    config[key].update(val_copy)
                    config[key]["reference_lines"].update(rl)
                elif key == "daily_summary":
                    config[key] = copy.deepcopy(_DEFAULTS[key])
                    user_daily = copy.deepcopy(val)
                    user_thresholds = user_daily.pop("thresholds", {})
                    config[key].update(user_daily)
                    config[key]["thresholds"].update(user_thresholds)
                else:
                    config[key].update(val)
            else:
                config[key] = val

    # products のキーを int に統一
    raw_products: dict = config.get("products", {})
    config["products"] = {int(k): v for k, v in raw_products.items()}

    # product_names のキーを int に統一
    raw_names: dict = config.get("product_names", {})
    config["product_names"] = {int(k): str(v) for k, v in raw_names.items()}

    # products が定義されていれば product_names に反映（後方互換）
    for num, info in config["products"].items():
        config["product_names"].setdefault(int(num), str(info.get("name", f"品種番号{num}")))

    # リスト値を str のセットに正規化
    config["ok_rank_codes"] = [str(x) for x in config.get("ok_rank_codes", ["2"])]
    config["ok_pass_senbetsu"] = [
        str(x) for x in config.get("ok_pass_senbetsu", ["1"])
    ]

    _cached = config
    return config


def get_product_name(hinshoku_num: int, config: dict[str, Any] | None = None) -> str:
    """品種番号から製品名を返す。未登録の場合は「品種番号{N}」を返す。"""
    if config is None:
        config = load_config()
    return config["product_names"].get(int(hinshoku_num), f"品種番号{hinshoku_num}")


def save_product_names(names: dict[int, str]) -> None:
    """product_names セクションのみを config.yaml に保存する。products の name も同期する。"""
    global _cached
    path = get_user_config_path()
    raw: dict = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    raw["product_names"] = {str(k): str(v) for k, v in sorted(names.items())}
    # products セクションの name を同期
    products = raw.get("products", {})
    for k, name in names.items():
        sk = str(k)
        if sk in products and isinstance(products[sk], dict):
            products[sk]["name"] = name
    if products:
        raw["products"] = products
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    _cached = None


def save_products(products: dict[int, dict]) -> None:
    """products / product_names セクションをユーザー config.yaml に保存する。"""
    global _cached
    path = get_user_config_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw: dict = yaml.safe_load(f) or {}
    else:
        raw = {}
    raw["products"] = {
        str(k): {kk: vv for kk, vv in v.items()}
        for k, v in sorted(products.items())
    }
    raw["product_names"] = {
        str(k): str(v.get("name", f"品種番号{k}"))
        for k, v in sorted(products.items())
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    _cached = None  # 次回 load_config で再読み込みさせる
