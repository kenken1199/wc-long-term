# =========================
# ■ CSV正規化（既存ツールから抽出・共通化）
# =========================
"""
アンリツ・イシダ両形式のCSVを統一フォーマットに正規化するモジュール。
既存の重量分析ツールから抽出。RECORDフォルダ集計ツールでも使用する。
"""

import os
import pandas as pd

_ISHIDA_RANK_VALUES = {"正量", "軽量", "過量"}


def _read_csv(file, **kwargs):
    """エンコーディング自動判別でCSV読込"""
    for enc in ("cp932", "utf-8-sig"):
        try:
            return pd.read_csv(file, encoding=enc, **kwargs)
        except UnicodeDecodeError:
            continue
    raise ValueError(
        f"CSVのエンコーディングを判別できませんでした: {os.path.basename(file)}"
    )


def normalize_columns(file, keep_hinshoku_column=False):
    """
    アンリツ・イシダ両形式に対応した正規化処理。

    Args:
        keep_hinshoku_column: Trueの場合、戻りdfに「品種番号」列を残す。
                              （RECORDフォルダ集計用：品種混在ファイルを
                              品種別に分割するために必要。既存の
                              重量分析ツールは False のまま使用してOK）

    Returns:
        (df, hinshoku_num)
        df: 正規化済みDataFrame
            列: 測定値出力No., 日付時刻, 測定値(g), ランクコード, メーカー
            keep_hinshoku_column=Trueの場合は「品種番号」列を追加
        hinshoku_num: ファイル代表の品種番号（int）または None
                     （複数品種混在の場合は最初の有効値）
    """
    df = _read_csv(file)
    df.columns = df.columns.str.replace("　", "").str.replace(" ", "").str.strip()
    df = df.loc[:, ~df.columns.duplicated()]
    cols = df.columns.tolist()

    # ===== アンリツ =====
    if any("測定値" in col for col in cols) and any("ランクコード" in col for col in cols):
        rename_map = {}
        for col in cols:
            if col == "測定値出力No.":
                rename_map[col] = "測定値出力No."
            elif col == "測定値(g)":
                rename_map[col] = "測定値(g)"
            elif "ランクコード" in col:
                rename_map[col] = "ランクコード"
            elif "日付時刻" in col:
                rename_map[col] = "日付時刻"

        df = df.rename(columns=rename_map)

        if "日付時刻" not in df.columns:
            if "日付" in df.columns and "時刻" in df.columns:
                df["日付時刻"] = pd.to_datetime(
                    df["日付"].astype(str) + " " + df["時刻"].astype(str),
                    errors="coerce"
                )

        hinshoku_num = None
        hinshoku_series = None
        if "品種" in df.columns:
            hinshoku_series = pd.to_numeric(df["品種"], errors="coerce")
            vals = hinshoku_series.dropna()
            if len(vals) > 0:
                hinshoku_num = int(vals.iloc[0])

        df["メーカー"] = "アンリツ"

        keep_cols = ["測定値出力No.", "日付時刻", "測定値(g)", "ランクコード", "メーカー"]
        if keep_hinshoku_column:
            df["品種番号"] = hinshoku_series if hinshoku_series is not None else pd.NA
            keep_cols.append("品種番号")

        df = df[keep_cols].copy()
        df["測定値出力No."] = pd.to_numeric(df["測定値出力No."], errors="coerce")
        df["測定値(g)"] = pd.to_numeric(df["測定値(g)"], errors="coerce")
        df["ランクコード"] = df["ランクコード"].astype(str).str.strip()
        df["日付時刻"] = pd.to_datetime(df["日付時刻"], errors="coerce")
        return df, hinshoku_num

    # ===== イシダ判定 =====
    df_ishida = _read_csv(file, skiprows=10)
    df_ishida.columns = df_ishida.columns.str.replace("　", "").str.replace(" ", "").str.strip()

    is_ishida = (
        len(df_ishida.columns) >= 6
        and df_ishida.iloc[:, 5].dropna().astype(str).isin(_ISHIDA_RANK_VALUES).any()
    )
    if not is_ishida:
        raise ValueError(
            f"未対応のCSVフォーマットです。アンリツまたはイシダ形式のCSVを選択してください。\n"
            f"ファイル: {os.path.basename(file)}"
        )

    hinshoku_num = None
    hinshoku_series = None
    if "予約番号" in df_ishida.columns:
        hinshoku_series = pd.to_numeric(df_ishida["予約番号"], errors="coerce")
        vals = hinshoku_series.dropna()
        if len(vals) > 0:
            hinshoku_num = int(vals.iloc[0])
    elif len(df_ishida.columns) >= 4:
        hinshoku_series = pd.to_numeric(df_ishida.iloc[:, 3], errors="coerce")
        vals = hinshoku_series.dropna()
        if len(vals) > 0:
            hinshoku_num = int(vals.iloc[0])

    df_out = df_ishida.iloc[:, [0, 1, 4, 5]].copy()
    df_out.columns = ["日付", "時刻", "測定値(g)", "判定"]

    df_out["日付時刻"] = pd.to_datetime(
        df_out["日付"].astype(str) + " " + df_out["時刻"].astype(str),
        errors="coerce"
    )
    df_out["測定値出力No."] = range(1, len(df_out) + 1)
    rank_map = {"正量": "2", "軽量": "1", "過量": "E"}
    df_out["ランクコード"] = df_out["判定"].map(rank_map)
    df_out["メーカー"] = "イシダ"

    keep_cols = ["測定値出力No.", "日付時刻", "測定値(g)", "ランクコード", "メーカー"]
    if keep_hinshoku_column:
        if hinshoku_series is not None:
            df_out["品種番号"] = hinshoku_series.values
        else:
            df_out["品種番号"] = pd.NA
        keep_cols.append("品種番号")

    return df_out[keep_cols].copy(), hinshoku_num
