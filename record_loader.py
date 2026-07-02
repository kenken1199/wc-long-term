"""
==========================================================
 品種別詳細データの読込・集計モジュール
==========================================================

 集計済みインデックス（aggregate_by_hinshokuの結果）を元に、
 元のCSVファイルを再読込して詳細分析用のDataFrameを構築する。

 ・全期間の生OKデータ（ヒストグラム・統合分析用）
 ・日別集計（経時変化グラフ用）

 都度CSV再読込方式：低メモリだが実行時に時間がかかる
==========================================================
"""

import os
import numpy as np
import pandas as pd

from csv_normalizer import normalize_columns


def load_hinshoku_data(record_dir, hinshoku_num, file_list, progress_callback=None):
    """
    指定された品種番号に該当するCSVデータをすべて読込み、
    その品種のレコードだけ抜き出して結合したDataFrameを返す。

    Args:
        record_dir: RECORDフォルダのルート
        hinshoku_num: 抽出する品種番号
        file_list: aggregate_by_hinshoku結果の_file_list（その品種に関わるファイル群）
        progress_callback: callable(current, total, filename) - 進捗通知

    Returns:
        df: 結合済みDataFrame（測定値出力No., 日付時刻, 測定値(g),
                              ランクコード, メーカー, 品種番号, 元ファイル）
            日付時刻順でソート済み
    """
    # 元ファイル単位でユニーク化（複数品種混在ファイルの重複読込を防ぐ）
    unique_relpaths = sorted({
        f.get("relpath", f.get("filename", ""))
        for f in file_list
    })

    dfs = []
    total = len(unique_relpaths)
    errors = []

    for i, relpath in enumerate(unique_relpaths):
        if progress_callback:
            progress_callback(i + 1, total, relpath)

        abspath = os.path.join(record_dir, relpath)
        if not os.path.exists(abspath):
            errors.append((relpath, "ファイルが見つかりません"))
            continue

        try:
            df, _ = normalize_columns(abspath, keep_hinshoku_column=True)
            df["品種番号"] = pd.to_numeric(
                df["品種番号"], errors="coerce"
            ).fillna(-1).astype(int)
            # 該当品種だけ抜き出し
            df = df[df["品種番号"] == hinshoku_num].copy()
            if len(df) > 0:
                df["元ファイル"] = relpath
                dfs.append(df)
        except Exception as e:
            errors.append((relpath, str(e)))

    if not dfs:
        return pd.DataFrame(), errors

    combined = pd.concat(dfs, ignore_index=True)
    combined = combined.sort_values("日付時刻").reset_index(drop=True)

    return combined, errors


def aggregate_by_date_folder(file_list):
    """
    日付フォルダ単位での集計を行う。
    元CSVを読まずに、キャッシュ済みのfile_listだけから計算する（高速）。

    Args:
        file_list: aggregate_by_hinshoku結果の_file_list

    Returns:
        DataFrame（日付フォルダ順）
        列: 日付, 総件数, OK件数, NG件数, 不良率(%), 平均(g),
            σ(g), Min(g), Max(g)
    """
    from collections import defaultdict
    by_date = defaultdict(list)
    for f in file_list:
        date = f["date_folder"]
        by_date[date].append(f)

    rows = []
    for date in sorted(by_date.keys()):
        entries = by_date[date]

        total = sum(e.get("総件数", 0) for e in entries)
        ok = sum(e.get("OK件数", 0) for e in entries)
        ng = total - ok
        defect_rate = (ng / total * 100) if total > 0 else 0.0

        # 平均・σは件数で重み付けして合成
        means = []
        stds = []
        ns = []
        mins = []
        maxs = []
        for e in entries:
            if e.get("平均") is not None and e.get("OK件数", 0) >= 1:
                means.append(e["平均"])
                stds.append(e.get("σ") or 0.0)
                ns.append(e["OK件数"])
                if e.get("Min") is not None:
                    mins.append(e["Min"])
                if e.get("Max") is not None:
                    maxs.append(e["Max"])

        if sum(ns) >= 2:
            ns_arr = np.array(ns, dtype=float)
            means_arr = np.array(means, dtype=float)
            stds_arr = np.array(stds, dtype=float)
            n_total = ns_arr.sum()
            grand_mean = float(np.sum(ns_arr * means_arr) / n_total)
            within = np.sum((ns_arr - 1) * stds_arr ** 2)
            between = np.sum(ns_arr * (means_arr - grand_mean) ** 2)
            grand_var = (within + between) / (n_total - 1)
            grand_std = float(np.sqrt(grand_var))
        elif means:
            grand_mean = means[0]
            grand_std = None
        else:
            grand_mean = None
            grand_std = None

        rows.append({
            "日付": date,
            "ファイル数": len(entries),
            "総件数": total,
            "OK件数": ok,
            "NG件数": ng,
            "不良率(%)": defect_rate,
            "平均(g)": grand_mean,
            "σ(g)": grand_std,
            "Min(g)": min(mins) if mins else None,
            "Max(g)": max(maxs) if maxs else None,
        })

    return pd.DataFrame(rows)


def detect_abnormal_dates(daily_df, mean_threshold=2.0, std_threshold=2.0):
    """
    日別集計から異常日を検出する。
    全期間の平均±2σから外れた日、または σ が他日より大きい日。

    Args:
        daily_df: aggregate_by_date_folderの結果
        mean_threshold: 平均値の偏差倍率（デフォルト2σ）
        std_threshold: σ自体の偏差倍率

    Returns:
        DataFrame: daily_dfに「異常フラグ」「異常理由」列を追加
    """
    df = daily_df.copy()
    df["異常フラグ"] = False
    df["異常理由"] = ""

    valid = df.dropna(subset=["平均(g)"])
    if len(valid) < 3:
        return df

    overall_mean = valid["平均(g)"].mean()
    overall_std = valid["平均(g)"].std(ddof=1) if len(valid) >= 2 else 0

    if overall_std > 0:
        for idx in df.index:
            reasons = []
            mean_val = df.at[idx, "平均(g)"]
            if mean_val is not None and not pd.isna(mean_val):
                deviation = abs(mean_val - overall_mean) / overall_std
                if deviation > mean_threshold:
                    reasons.append(f"平均が他日と{deviation:.1f}σずれ")

            if reasons:
                df.at[idx, "異常フラグ"] = True
                df.at[idx, "異常理由"] = " / ".join(reasons)

    # σの異常も検出
    valid_std = df.dropna(subset=["σ(g)"])
    if len(valid_std) >= 3:
        std_mean = valid_std["σ(g)"].mean()
        std_std = valid_std["σ(g)"].std(ddof=1)
        if std_std > 0:
            for idx in df.index:
                sigma_val = df.at[idx, "σ(g)"]
                if sigma_val is not None and not pd.isna(sigma_val):
                    deviation = (sigma_val - std_mean) / std_std
                    if deviation > std_threshold:
                        existing = df.at[idx, "異常理由"]
                        new_reason = f"σが他日より{deviation:.1f}σ大"
                        df.at[idx, "異常フラグ"] = True
                        df.at[idx, "異常理由"] = (
                            f"{existing} / {new_reason}" if existing else new_reason
                        )

    return df


def compute_overall_stats(combined_df, reject_limit=None, nominal_weight=None):
    """
    結合済みDataFrameから全期間統計を計算（生データから直接）。

    Args:
        reject_limit:   排除上下限(g)。指定時に Pp/Ppk を計算。
        nominal_weight: 公称重量(g)。指定時に Ppk を計算。

    Returns:
        dict: 平均, σ（全体）, Pp/Ppk（全体σ使用）, ほか
    """
    from scipy import stats as scipy_stats

    ok_data = pd.to_numeric(
        combined_df.loc[combined_df["ランクコード"] == "2", "測定値(g)"],
        errors="coerce"
    ).dropna()

    n = len(ok_data)
    if n < 2:
        return None

    mean = float(ok_data.mean())
    std_overall = float(ok_data.std(ddof=1))
    vmin = float(ok_data.min())
    vmax = float(ok_data.max())

    lower = mean - 3 * std_overall
    upper = mean + 3 * std_overall

    t_value = scipy_stats.t.ppf(0.975, df=n - 1)
    margin = t_value * std_overall / np.sqrt(n)
    ci_lower = mean - margin
    ci_upper = mean + margin

    outlier_count = int(((ok_data < lower) | (ok_data > upper)).sum())

    # Pp/Ppk: σ_overall（全体σ）使用 ← 複数ロット・複数日の実績性能
    mean_bias = abs(mean - nominal_weight) if nominal_weight is not None else None

    pp, ppk = None, None
    if reject_limit is not None and std_overall > 0:
        pp = reject_limit / (3 * std_overall)
        if mean_bias is not None:
            ppk = (reject_limit - mean_bias) / (3 * std_overall)

    return {
        "件数": n,
        "平均": mean,
        "σ": std_overall,
        "Min": vmin,
        "Max": vmax,
        "推奨下限": lower,
        "推奨上限": upper,
        "CI下限": ci_lower,
        "CI上限": ci_upper,
        "外れ値件数": outlier_count,
        "Pp": pp,
        "Ppk": ppk,
    }
