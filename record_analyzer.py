"""
==========================================================
 RECORD フォルダ集計ツール（フェーズ1）
==========================================================

 アンリツ計量機の RECORD フォルダを横断スキャンし、
 品種番号ごとに統計値を集計する独立ツール。

 機能（フェーズ1）:
   - RECORDフォルダ選択
   - 全CSVスキャン（バックグラウンドスレッド・進捗表示）
   - 同日・同品種のINDIV*.csv自動結合
   - インデックスキャッシュ（mtime差分更新）
   - 品種番号一覧表示（ソート・検索）

 想定フォルダ構造:
   RECORD/
   ├── 20250219/
   │   ├── INDIV.csv
   │   └── INDIV_01.csv  (容量分割 - 同品種の場合は結合)
   ├── 20250220/
   │   └── INDIV.csv
   ...

 キャッシュ: RECORDフォルダ直下に record_cache.json
==========================================================
"""

import os
import re
import json
import threading
import queue
import datetime
import traceback
from collections import defaultdict

import numpy as np
import pandas as pd

import font_utils

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from record_dialog import HinshokuDetailDialog
except ImportError:
    import types
    _stub = object
    tk = types.SimpleNamespace(
        Tk=_stub, Toplevel=_stub, Frame=_stub,
        StringVar=_stub, IntVar=_stub,
    )
    ttk = types.SimpleNamespace(
        Frame=_stub, Button=_stub, Label=_stub, Entry=_stub,
        Treeview=_stub, Scrollbar=_stub, Progressbar=_stub,
        Spinbox=_stub, Notebook=_stub,
    )
    messagebox = None
    filedialog = None
    HinshokuDetailDialog = None

from csv_normalizer import normalize_columns
from config_loader import load_config, save_product_names


# =========================
# ■ 定数
# =========================
CACHE_FILENAME = "record_cache.json"
CACHE_VERSION = 2  # v2: ファイル内複数品種に対応（品種ごとに分割保存）
PRODUCT_NAMES_FILENAME = "product_names.csv"


DATE_FOLDER_PATTERN = re.compile(r"^\d{8}$")  # 例: 20250219
INDIV_PATTERN = re.compile(r"^INDIV(_\d+)?\.csv$", re.IGNORECASE)

font_utils.setup_japanese_font()


# =========================
# ■ スキャン処理（バックグラウンド）
# =========================
def find_indiv_csvs(record_dir):
    """
    RECORDフォルダ配下のINDIV*.csvを列挙する。

    Returns:
        list of (relpath, abspath, mtime, size, date_folder)
        relpath: RECORDからの相対パス（キャッシュキー用）
    """
    found = []
    for entry in sorted(os.listdir(record_dir)):
        date_folder_path = os.path.join(record_dir, entry)
        if not os.path.isdir(date_folder_path):
            continue
        if not DATE_FOLDER_PATTERN.match(entry):
            continue
        for fname in sorted(os.listdir(date_folder_path)):
            if not INDIV_PATTERN.match(fname):
                continue
            abspath = os.path.join(date_folder_path, fname)
            relpath = os.path.relpath(abspath, record_dir).replace("\\", "/")
            try:
                stat = os.stat(abspath)
            except OSError:
                continue
            found.append({
                "relpath": relpath,
                "abspath": abspath,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "date_folder": entry,
                "filename": fname,
            })
    return found


def analyze_csv_file(abspath):
    """
    1つのCSVから品種番号別に統計値を抽出する。
    1ファイル内に複数品種が混在する場合、品種ごとに分けて統計を計算する。

    Returns:
        list of dict（品種ごとの統計値）
        各dictは以下のキーを持つ:
            品種番号, 総件数, OK件数, NG件数, 平均, σ, Min, Max,
            開始, 終了, ランクコード別

    Raises:
        Exception: CSVが読めない場合（呼び出し側でcatch）
    """
    df, _ = normalize_columns(abspath, keep_hinshoku_column=True)
    # 品種番号NaNは-1（不明）に統一
    if "品種番号" in df.columns:
        df["品種番号"] = pd.to_numeric(df["品種番号"], errors="coerce").fillna(-1).astype(int)
    else:
        df["品種番号"] = -1

    if len(df) == 0:
        return []

    results = []
    # 品種番号でグループ化（1ファイル内に複数品種があればここで分かれる）
    for hinshoku, group in df.groupby("品種番号"):
        total_count = len(group)
        ok_mask = group["ランクコード"].astype(str) == "2"
        ok_count = int(ok_mask.sum())
        ng_count = int((~ok_mask).sum())

        # OKデータの統計値
        ok_data = pd.to_numeric(group.loc[ok_mask, "測定値(g)"], errors="coerce").dropna()
        if len(ok_data) >= 2:
            mean = float(ok_data.mean())
            std = float(ok_data.std(ddof=1))
            vmin = float(ok_data.min())
            vmax = float(ok_data.max())
        elif len(ok_data) == 1:
            mean = float(ok_data.iloc[0])
            std = 0.0
            vmin = mean
            vmax = mean
        else:
            mean = std = vmin = vmax = None

        # 開始・終了時刻
        valid_dt = group["日付時刻"].dropna()
        start = valid_dt.min().isoformat() if len(valid_dt) > 0 else None
        end = valid_dt.max().isoformat() if len(valid_dt) > 0 else None

        # ランクコード別件数
        rank_counts = group["ランクコード"].astype(str).value_counts().to_dict()

        results.append({
            "品種番号": int(hinshoku),
            "総件数": int(total_count),
            "OK件数": int(ok_count),
            "NG件数": int(ng_count),
            "平均": mean,
            "σ": std,
            "Min": vmin,
            "Max": vmax,
            "開始": start,
            "終了": end,
            "ランクコード別": rank_counts,
        })

    return results


def scan_record_folder(record_dir, progress_queue, cancel_event):
    """
    RECORDフォルダをスキャンしてキャッシュを更新する。
    バックグラウンドスレッドで実行される。

    progress_queue: ("progress", current, total, filename) や
                    ("done", index_data) や
                    ("error", message) を送る
    """
    try:
        # 既存キャッシュ読込
        cache_path = os.path.join(record_dir, CACHE_FILENAME)
        old_cache = {}
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                    if cached.get("version") == CACHE_VERSION:
                        old_cache = cached.get("files", {})
            except (json.JSONDecodeError, OSError):
                old_cache = {}

        # ファイル列挙
        progress_queue.put(("progress", 0, 0, "ファイル列挙中..."))
        files = find_indiv_csvs(record_dir)
        total = len(files)

        if total == 0:
            progress_queue.put(("error",
                "INDIV.csvが見つかりません。\nRECORDフォルダの構造をご確認ください。"))
            return

        new_cache = {}
        errors = []
        reused = 0
        rescanned = 0

        for i, file_info in enumerate(files):
            if cancel_event.is_set():
                progress_queue.put(("cancelled", None))
                return

            relpath = file_info["relpath"]
            mtime = file_info["mtime"]
            size = file_info["size"]

            progress_queue.put(("progress", i + 1, total, relpath))

            # キャッシュヒット判定: 同じrelpathから始まるエントリがすべて
            # 同じmtime/sizeなら再利用（ファイルが更新されていない）
            cached_entries = {
                k: v for k, v in old_cache.items()
                if v.get("relpath") == relpath
                and v.get("mtime") == mtime
                and v.get("size") == size
            }
            if cached_entries:
                new_cache.update(cached_entries)
                reused += 1
                continue

            # 再スキャン（品種別の複数エントリが返る）
            try:
                hinshoku_entries = analyze_csv_file(file_info["abspath"])
                if not hinshoku_entries:
                    errors.append((relpath, "有効なデータがありません"))
                    continue

                # 品種ごとに別エントリとしてキャッシュに登録
                # キー: relpath#品種番号（複数品種混在ファイルを区別）
                for entry in hinshoku_entries:
                    h = entry["品種番号"]
                    cache_key = f"{relpath}#{h}"
                    new_cache[cache_key] = {
                        "relpath": relpath,
                        "mtime": mtime,
                        "size": size,
                        "date_folder": file_info["date_folder"],
                        "filename": file_info["filename"],
                        **entry,
                    }
                rescanned += 1
            except Exception as e:
                errors.append((relpath, str(e)))

        # キャッシュ保存
        cache_data = {
            "version": CACHE_VERSION,
            "scanned_at": datetime.datetime.now().isoformat(),
            "record_dir": record_dir,
            "files": new_cache,
        }
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            errors.append(("(キャッシュ保存)", str(e)))

        progress_queue.put(("done", {
            "files": new_cache,
            "reused": reused,
            "rescanned": rescanned,
            "errors": errors,
            "record_dir": record_dir,
        }))

    except Exception as e:
        progress_queue.put(("error",
            f"スキャン中に予期せぬエラー:\n{e}\n\n{traceback.format_exc()}"))


# =========================
# ■ 品種番号ごとの集計
# =========================
def aggregate_by_hinshoku(file_index):
    """
    ファイル単位のインデックスを品種番号単位にロールアップする。
    同一品種の複数日・複数ファイルを統合した統計値を計算する。

    Returns:
        list of dict（品種番号順）
    """
    by_hinshoku = defaultdict(list)
    for relpath, info in file_index.items():
        h = info.get("品種番号", -1)
        by_hinshoku[h].append(info)

    results = []
    for hinshoku, file_list in by_hinshoku.items():
        # 製造日（date_folder）の集合
        date_folders = sorted({f["date_folder"] for f in file_list})

        # ファイル数: 元のCSVファイル単位でユニークカウント
        # （1つのCSVに複数品種混在の場合、その品種に関わるファイルだけ数える）
        unique_files = {f.get("relpath", f.get("filename")) for f in file_list}
        file_count = len(unique_files)

        total_count = sum(f.get("総件数", 0) for f in file_list)
        total_ok = sum(f.get("OK件数", 0) for f in file_list)
        total_ng = sum(f.get("NG件数", 0) for f in file_list)

        # 加重平均と統合σを計算（OK件数で重み付け）
        # ファイル単位の (n_i, mean_i, std_i) から全体の (mean, std) を合成
        means = []
        stds = []
        ns = []
        mins = []
        maxs = []
        for f in file_list:
            if f.get("平均") is not None and f.get("OK件数", 0) >= 1:
                means.append(f["平均"])
                stds.append(f.get("σ") or 0.0)
                ns.append(f["OK件数"])
                if f.get("Min") is not None:
                    mins.append(f["Min"])
                if f.get("Max") is not None:
                    maxs.append(f["Max"])

        if sum(ns) >= 2:
            ns_arr = np.array(ns, dtype=float)
            means_arr = np.array(means, dtype=float)
            stds_arr = np.array(stds, dtype=float)
            n_total = ns_arr.sum()
            grand_mean = float(np.sum(ns_arr * means_arr) / n_total)
            # 統合分散の公式: 各群の分散 + 平均の偏差の二乗
            within = np.sum((ns_arr - 1) * stds_arr**2)
            between = np.sum(ns_arr * (means_arr - grand_mean)**2)
            grand_var = (within + between) / (n_total - 1)
            grand_std = float(np.sqrt(grand_var))
            overall_min = float(min(mins)) if mins else None
            overall_max = float(max(maxs)) if maxs else None
        else:
            grand_mean = means[0] if means else None
            grand_std = None
            overall_min = mins[0] if mins else None
            overall_max = maxs[0] if maxs else None

        defect_rate = (total_ng / total_count * 100) if total_count > 0 else 0.0

        # 推奨規格（平均±3σ）
        if grand_mean is not None and grand_std is not None and grand_std > 0:
            spec_lower = grand_mean - 3 * grand_std
            spec_upper = grand_mean + 3 * grand_std
        else:
            spec_lower = spec_upper = None

        # 最終製造日
        last_date = date_folders[-1] if date_folders else None
        first_date = date_folders[0] if date_folders else None

        results.append({
            "品種番号": hinshoku,
            "製造日数": len(date_folders),
            "ファイル数": file_count,
            "総件数": total_count,
            "OK件数": total_ok,
            "NG件数": total_ng,
            "不良率(%)": defect_rate,
            "平均(g)": grand_mean,
            "σ(g)": grand_std,
            "Min(g)": overall_min,
            "Max(g)": overall_max,
            "推奨下限(g)": spec_lower,
            "推奨上限(g)": spec_upper,
            "初回製造日": first_date,
            "最終製造日": last_date,
            "_date_folders": date_folders,  # 詳細表示用
            "_file_list": file_list,        # 詳細表示用
        })

    # 品種番号でソート（-1=不明は最後）
    results.sort(key=lambda r: (r["品種番号"] < 0, r["品種番号"]))
    return results


# =========================
# ■ メインアプリ
# =========================
class RecordAnalyzerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("RECORD集計")
        self.root.geometry("1200x700")

        self.record_dir = None
        self.file_index = {}      # relpath -> info
        self.aggregates = []      # aggregate_by_hinshokuの結果
        self.product_names = load_config().get("product_names", {})  # 品種番号 -> 製品名
        self.scan_queue = None
        self.cancel_event = None

        self._build_ui()

    # -------------------------
    # UI構築
    # -------------------------
    def _build_ui(self):
        self._build_record_tab(self.root)

    def _build_record_tab(self, parent):
        # トップバー
        top = ttk.Frame(parent, padding=10)
        top.pack(fill="x")

        ttk.Button(top, text="📁 RECORDフォルダを選択",
                   command=self._on_select_folder).pack(side="left")

        self.folder_label_var = tk.StringVar(value="（フォルダ未選択）")
        ttk.Label(top, textvariable=self.folder_label_var,
                  foreground="gray").pack(side="left", padx=10)

        ttk.Button(top, text="🏷 製品名マスター",
                   command=self._on_open_product_names).pack(side="right", padx=(0, 5))
        ttk.Button(top, text="🔄 再スキャン",
                   command=self._on_rescan).pack(side="right")

        # 検索バー
        search_bar = ttk.Frame(parent, padding=(10, 0, 10, 5))
        search_bar.pack(fill="x")
        ttk.Label(search_bar, text="🔍 品種番号/製品名で絞込:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refresh_tree())
        ttk.Entry(search_bar, textvariable=self.search_var, width=15).pack(side="left", padx=5)

        self.summary_var = tk.StringVar()
        ttk.Label(search_bar, textvariable=self.summary_var,
                  foreground="navy").pack(side="left", padx=20)

        ttk.Label(search_bar, text="💡 行をダブルクリックで詳細表示",
                  foreground="gray").pack(side="right")

        # ステータスバー（先にpackしてtree_frameのexpandが正しく機能するようにする）
        self.status_var = tk.StringVar(value="RECORDフォルダを選択してください")
        status = ttk.Label(parent, textvariable=self.status_var,
                          relief="sunken", anchor="w", padding=(5, 2))
        status.pack(fill="x", side="bottom")

        # Treeview
        tree_frame = ttk.Frame(parent, padding=(10, 0, 10, 10))
        tree_frame.pack(fill="both", expand=True)

        cols = ("品種番号", "製品名", "製造日数", "総件数", "OK件数", "不良率(%)",
                "平均(g)", "σ(g)", "Min(g)", "Max(g)",
                "推奨下限(g)", "推奨上限(g)", "最終製造日")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=20)

        col_widths = {
            "品種番号": 75, "製品名": 150, "製造日数": 65, "総件数": 75, "OK件数": 75,
            "不良率(%)": 70, "平均(g)": 75, "σ(g)": 70,
            "Min(g)": 70, "Max(g)": 70,
            "推奨下限(g)": 85, "推奨上限(g)": 85, "最終製造日": 95,
        }

        col_labels = {
            "推奨下限(g)": "下限 −3σ (g)",
            "推奨上限(g)": "上限 +3σ (g)",
        }
        for col in cols:
            self.tree.heading(col, text=col_labels.get(col, col),
                              command=lambda c=col: self._sort_by(c))
            anchor = "center" if col in ("品種番号", "製造日数", "最終製造日") else "e"
            if col == "製品名":
                anchor = "w"
            self.tree.column(col, anchor=anchor, width=col_widths[col])

        self.tree.tag_configure("error", background="#FFE4E1")

        # ダブルクリック / Enter で詳細画面を開く
        self.tree.bind("<Double-1>", self._on_row_activate)
        self.tree.bind("<Return>", self._on_row_activate)

        sb_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        sb_x = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb_y.grid(row=0, column=1, sticky="ns")
        sb_x.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self._sort_state = {}  # col -> ascending bool

    # -------------------------
    # フォルダ選択 / スキャン起動
    # -------------------------
    def _on_select_folder(self):
        folder = filedialog.askdirectory(title="RECORDフォルダを選択")
        if not folder:
            return
        self.record_dir = folder
        self.folder_label_var.set(folder)
        self._start_scan()

    def _on_rescan(self):
        if not self.record_dir:
            messagebox.showinfo("情報", "先にRECORDフォルダを選択してください")
            return
        self._start_scan()

    def _start_scan(self):
        # 進捗ダイアログを開いてバックグラウンドスレッドを開始
        self.scan_queue = queue.Queue()
        self.cancel_event = threading.Event()

        progress_dialog = ProgressDialog(self.root, self.cancel_event)

        thread = threading.Thread(
            target=scan_record_folder,
            args=(self.record_dir, self.scan_queue, self.cancel_event),
            daemon=True,
        )
        thread.start()

        self._poll_scan(progress_dialog)

    def _poll_scan(self, progress_dialog):
        try:
            while True:
                msg = self.scan_queue.get_nowait()
                kind = msg[0]

                if kind == "progress":
                    _, current, total, fname = msg
                    progress_dialog.update_progress(current, total, fname)

                elif kind == "done":
                    progress_dialog.close()
                    self._on_scan_complete(msg[1])
                    return

                elif kind == "cancelled":
                    progress_dialog.close()
                    self.status_var.set("スキャンをキャンセルしました")
                    return

                elif kind == "error":
                    progress_dialog.close()
                    messagebox.showerror("スキャンエラー", msg[1])
                    self.status_var.set("スキャンに失敗しました")
                    return

        except queue.Empty:
            pass

        # 100ms後にもう一度ポーリング
        self.root.after(100, lambda: self._poll_scan(progress_dialog))

    def _on_scan_complete(self, result):
        self.file_index = result["files"]
        reused = result["reused"]
        rescanned = result["rescanned"]
        errors = result["errors"]

        self.aggregates = aggregate_by_hinshoku(self.file_index)
        self._migrate_product_names_csv()
        self._reload_product_names()
        self._refresh_tree()

        n_hinshoku = len(self.aggregates)
        msg = (f"スキャン完了: {len(self.file_index)}ファイル "
               f"(キャッシュ再利用 {reused} / 新規/更新 {rescanned})")
        if errors:
            msg += f" / エラー {len(errors)}件"
        self.status_var.set(msg)

        if errors:
            err_text = "\n".join(f"  ・{r}: {e}" for r, e in errors[:10])
            if len(errors) > 10:
                err_text += f"\n  ...他 {len(errors) - 10}件"
            messagebox.showwarning(
                "一部のファイルが読み込めませんでした",
                f"以下のファイルで問題が発生しました:\n\n{err_text}"
            )

        info_msg = f"✓ 品種数: {n_hinshoku} / ファイル数: {len(self.file_index)}"
        if rescanned > 0 and reused > 0:
            info_msg += f" / 差分更新: {rescanned}件"
        self.summary_var.set(info_msg)

    # -------------------------
    # 製品名マスター
    # -------------------------
    def _reload_product_names(self):
        self.product_names = load_config(force_reload=True).get("product_names", {})

    def _on_open_product_names(self):
        dlg = ProductNamesDialog(self.root, self.product_names)
        self.root.wait_window(dlg)
        self._reload_product_names()
        self._refresh_tree()

    def _migrate_product_names_csv(self):
        """RECORD フォルダに product_names.csv があれば config.yaml に一度だけ移行する。"""
        if not self.record_dir:
            return
        csv_path = os.path.join(self.record_dir, PRODUCT_NAMES_FILENAME)
        if not os.path.exists(csv_path):
            return
        try:
            df = pd.read_csv(csv_path, dtype={"品種番号": str, "製品名": str})
            df["品種番号"] = pd.to_numeric(df["品種番号"], errors="coerce").dropna().astype(int)
            df = df.dropna(subset=["品種番号", "製品名"])
            csv_names = {int(k): str(v).strip() for k, v in zip(df["品種番号"], df["製品名"])}
        except Exception:
            return
        if not csv_names:
            return
        merged = dict(load_config().get("product_names", {}))
        merged.update(csv_names)
        save_product_names(merged)
        try:
            os.rename(csv_path, csv_path + ".migrated")
        except OSError:
            pass
        messagebox.showinfo(
            "製品名を移行しました",
            f"{len(csv_names)}件の製品名を config.yaml に移行しました。\n"
            f"旧ファイル: {PRODUCT_NAMES_FILENAME} → {PRODUCT_NAMES_FILENAME}.migrated",
            parent=self.root,
        )

    # -------------------------
    # Treeview 更新
    # -------------------------
    def _refresh_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        keyword = self.search_var.get().strip()

        for agg in self.aggregates:
            hinshoku = agg["品種番号"]
            product_name = self.product_names.get(hinshoku, "")
            if keyword and keyword not in str(hinshoku) and keyword not in product_name:
                continue

            tags = ()
            if hinshoku < 0:
                tags = ("error",)

            self.tree.insert("", "end", tags=tags, values=(
                hinshoku if hinshoku >= 0 else "(不明)",
                product_name,
                agg["製造日数"],
                f"{agg['総件数']:,}",
                f"{agg['OK件数']:,}",
                f"{agg['不良率(%)']:.2f}",
                f"{agg['平均(g)']:.3f}" if agg['平均(g)'] is not None else "-",
                f"{agg['σ(g)']:.4f}"  if agg['σ(g)']  is not None else "-",
                f"{agg['Min(g)']:.2f}" if agg['Min(g)'] is not None else "-",
                f"{agg['Max(g)']:.2f}" if agg['Max(g)'] is not None else "-",
                f"{agg['推奨下限(g)']:.2f}" if agg['推奨下限(g)'] is not None else "-",
                f"{agg['推奨上限(g)']:.2f}" if agg['推奨上限(g)'] is not None else "-",
                agg["最終製造日"] or "-",
            ))

    def _sort_by(self, col):
        """列ヘッダクリックでソート"""
        if not self.aggregates:
            return

        ascending = not self._sort_state.get(col, False)
        self._sort_state[col] = ascending

        key_map = {
            "品種番号":   lambda r: r["品種番号"],
            "製品名":     lambda r: self.product_names.get(r["品種番号"], ""),
            "製造日数":   lambda r: r["製造日数"],
            "総件数":     lambda r: r["総件数"],
            "OK件数":     lambda r: r["OK件数"],
            "不良率(%)":  lambda r: r["不良率(%)"],
            "平均(g)":    lambda r: r["平均(g)"] if r["平均(g)"] is not None else float("-inf"),
            "σ(g)":       lambda r: r["σ(g)"]    if r["σ(g)"]    is not None else float("-inf"),
            "Min(g)":     lambda r: r["Min(g)"]  if r["Min(g)"]  is not None else float("-inf"),
            "Max(g)":     lambda r: r["Max(g)"]  if r["Max(g)"]  is not None else float("-inf"),
            "推奨下限(g)": lambda r: r["推奨下限(g)"] if r["推奨下限(g)"] is not None else float("-inf"),
            "推奨上限(g)": lambda r: r["推奨上限(g)"] if r["推奨上限(g)"] is not None else float("-inf"),
            "最終製造日": lambda r: r["最終製造日"] or "",
        }
        if col in key_map:
            self.aggregates.sort(key=key_map[col], reverse=not ascending)
            self._refresh_tree()

    # -------------------------
    # 詳細画面起動
    # -------------------------
    def _on_row_activate(self, event=None):
        """Treeview行のダブルクリック/Enter押下で詳細画面を開く"""
        selected = self.tree.selection()
        if not selected:
            return
        item_id = selected[0]
        values = self.tree.item(item_id, "values")
        if not values:
            return

        # 1列目が品種番号文字列。"(不明)"はスキップ
        hinshoku_str = str(values[0])
        if hinshoku_str == "(不明)":
            messagebox.showinfo(
                "情報",
                "品種番号が不明な行は詳細表示できません。",
                parent=self.root,
            )
            return

        try:
            hinshoku_num = int(hinshoku_str)
        except ValueError:
            return

        # aggregateから対応する品種を探す
        agg = next(
            (a for a in self.aggregates if a["品種番号"] == hinshoku_num),
            None,
        )
        if agg is None:
            messagebox.showerror(
                "エラー",
                f"品種番号 {hinshoku_num} のデータが見つかりません",
                parent=self.root,
            )
            return

        product_name = self.product_names.get(hinshoku_num, "")

        # 詳細画面を開く（モードレス）
        HinshokuDetailDialog(self.root, self.record_dir, agg, product_name=product_name, config=load_config())


# =========================
# ■ 進捗ダイアログ
# =========================
class ProgressDialog(tk.Toplevel):
    def __init__(self, parent, cancel_event):
        super().__init__(parent)
        self.title("スキャン中...")
        self.cancel_event = cancel_event
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.grab_set()

        frm = ttk.Frame(self, padding=20)
        frm.pack()

        self.message_var = tk.StringVar(value="準備中...")
        ttk.Label(frm, textvariable=self.message_var,
                  width=60).pack(anchor="w", pady=(0, 5))

        self.progress = ttk.Progressbar(frm, length=400, mode="determinate")
        self.progress.pack(pady=5)

        self.detail_var = tk.StringVar(value="")
        ttk.Label(frm, textvariable=self.detail_var,
                  foreground="gray", width=60).pack(anchor="w", pady=(5, 10))

        ttk.Button(frm, text="キャンセル", command=self._on_cancel).pack()

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def update_progress(self, current, total, filename):
        if total > 0:
            pct = current / total * 100
            self.progress["value"] = pct
            self.message_var.set(f"スキャン中: {current} / {total} ファイル")
        else:
            self.progress["value"] = 0
            self.message_var.set("ファイル列挙中...")

        # 長いパスは末尾だけ表示
        display = filename if len(filename) < 70 else "..." + filename[-67:]
        self.detail_var.set(display)

    def _on_cancel(self):
        self.cancel_event.set()
        self.message_var.set("キャンセル中...")

    def close(self):
        self.destroy()


# =========================
# ■ 製品名マスターダイアログ
# =========================
class ProductNamesDialog(tk.Toplevel):
    def __init__(self, parent, current_names: dict):
        super().__init__(parent)
        self.title("製品名マスター")
        self.resizable(True, True)
        self.geometry("450x500")
        self.grab_set()

        self._names: dict[int, str] = {int(k): str(v) for k, v in current_names.items()}
        self._build_ui()
        self._refresh_tree()

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _build_ui(self):
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        tree_frm = ttk.Frame(frm)
        tree_frm.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(
            tree_frm, columns=("品種番号", "製品名"), show="headings", height=20
        )
        self.tree.heading("品種番号", text="品種番号")
        self.tree.heading("製品名", text="製品名")
        self.tree.column("品種番号", width=80, anchor="center")
        self.tree.column("製品名", width=300, anchor="w")

        sb = ttk.Scrollbar(tree_frm, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        tree_frm.rowconfigure(0, weight=1)
        tree_frm.columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", lambda e: self._on_edit())

        btn_frm = ttk.Frame(frm)
        btn_frm.pack(fill="x", pady=(8, 0))

        ttk.Button(btn_frm, text="追加", command=self._on_add).pack(side="left", padx=3)
        ttk.Button(btn_frm, text="編集", command=self._on_edit).pack(side="left", padx=3)
        ttk.Button(btn_frm, text="削除", command=self._on_delete).pack(side="left", padx=3)
        ttk.Button(btn_frm, text="保存して閉じる", command=self._on_save).pack(side="right", padx=3)
        ttk.Button(btn_frm, text="キャンセル", command=self.destroy).pack(side="right", padx=3)

    def _refresh_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for num in sorted(self._names.keys()):
            self.tree.insert("", "end", iid=str(num), values=(num, self._names[num]))

    def _on_add(self):
        dlg = _ProductNameEditDialog(self, None, None)
        self.wait_window(dlg)
        if dlg.result:
            num, name = dlg.result
            self._names[num] = name
            self._refresh_tree()

    def _on_edit(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("情報", "編集する行を選択してください", parent=self)
            return
        num = int(selected[0])
        name = self._names.get(num, "")
        dlg = _ProductNameEditDialog(self, num, name)
        self.wait_window(dlg)
        if dlg.result:
            new_num, new_name = dlg.result
            if new_num != num:
                del self._names[num]
            self._names[new_num] = new_name
            self._refresh_tree()

    def _on_delete(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("情報", "削除する行を選択してください", parent=self)
            return
        num = int(selected[0])
        name = self._names.get(num, "")
        if messagebox.askyesno("確認", f"品種番号 {num}「{name}」を削除しますか？", parent=self):
            del self._names[num]
            self._refresh_tree()

    def _on_save(self):
        save_product_names(self._names)
        self.destroy()


class _ProductNameEditDialog(tk.Toplevel):
    def __init__(self, parent, num, name):
        super().__init__(parent)
        self.title("製品名の編集" if num is not None else "製品名の追加")
        self.resizable(False, False)
        self.grab_set()
        self.result = None

        frm = ttk.Frame(self, padding=15)
        frm.pack()

        ttk.Label(frm, text="品種番号:").grid(row=0, column=0, sticky="e", pady=4)
        self._num_var = tk.StringVar(value=str(num) if num is not None else "")
        num_entry = ttk.Entry(frm, textvariable=self._num_var, width=10)
        num_entry.grid(row=0, column=1, sticky="w", padx=(5, 0), pady=4)

        ttk.Label(frm, text="製品名:").grid(row=1, column=0, sticky="e", pady=4)
        self._name_var = tk.StringVar(value=name or "")
        name_entry = ttk.Entry(frm, textvariable=self._name_var, width=30)
        name_entry.grid(row=1, column=1, sticky="w", padx=(5, 0), pady=4)

        btn_frm = ttk.Frame(frm)
        btn_frm.grid(row=2, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(btn_frm, text="OK", command=self._on_ok).pack(side="left", padx=5)
        ttk.Button(btn_frm, text="キャンセル", command=self.destroy).pack(side="left", padx=5)

        (num_entry if num is None else name_entry).focus_set()

        self.bind("<Return>", lambda e: self._on_ok())
        self.bind("<Escape>", lambda e: self.destroy())

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _on_ok(self):
        try:
            num = int(self._num_var.get().strip())
        except ValueError:
            messagebox.showerror("入力エラー", "品種番号は整数で入力してください", parent=self)
            return
        name = self._name_var.get().strip()
        if not name:
            messagebox.showerror("入力エラー", "製品名を入力してください", parent=self)
            return
        self.result = (num, name)
        self.destroy()


# =========================
# ■ エントリポイント
# =========================
if __name__ == "__main__":
    root = tk.Tk()
    app = RecordAnalyzerApp(root)

    # 画面中央に
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    w = root.winfo_width()
    h = root.winfo_height()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    root.mainloop()
