"""
==========================================================
 品種別詳細画面ダイアログ
==========================================================

 タブ構成:
   1. サマリー: 統計値テーブル + 統合ヒストグラム + 推奨規格
   2. 経時変化: 日別の平均/σ/不良率推移グラフ
   3. 日別一覧: 日付フォルダ単位の統計値テーブル
   4. データエクスポート: 全部入りExcel出力ボタン

 元データはCSV再読込方式（バックグラウンドスレッド）
==========================================================
"""

import os
import sys
import threading
import queue
import datetime

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from record_loader import (
    load_hinshoku_data,
    aggregate_by_date_folder,
    detect_abnormal_dates,
    compute_overall_stats,
)
from record_export import export_hinshoku_detail
from lot_analyze import LotPreviewDialog, process_lot, MIN_OK_COUNT


class HinshokuDetailDialog(tk.Toplevel):
    def __init__(self, parent, record_dir, aggregate_info, product_name="", config=None):
        super().__init__(parent)
        self.record_dir = record_dir
        self.aggregate_info = aggregate_info
        self.hinshoku_num = aggregate_info["品種番号"]
        self.product_name = product_name

        # config から排除限界・公称重量を取得（Cp/Cpk 計算用）
        product_info = (config or {}).get("products", {}).get(self.hinshoku_num, {})
        daily_cfg = (config or {}).get("daily_summary", {})
        self._reject_limit = float(
            product_info["reject_limit_g"]
            if "reject_limit_g" in product_info
            else daily_cfg.get("reject_limit_g", 1.2)
        )
        self._nominal_weight = (
            float(product_info["nominal_weight"])
            if "nominal_weight" in product_info
            else None
        )

        self.combined_df = None
        self.daily_df = None
        self.overall_stats = None

        self.title(f"品種詳細 - {self.display_name}")
        self.geometry("1100x720")
        self.resizable(True, True)

        self._build_ui()

        self.update_idletasks()
        x = parent.winfo_rootx() + 30
        y = parent.winfo_rooty() + 30
        self.geometry(f"+{x}+{y}")

        # 起動と同時にデータ読込開始
        self.after(100, self._start_load_data)

    @property
    def display_name(self):
        return self.product_name if self.product_name else f"品種番号{self.hinshoku_num}"

    # ------------------------------
    # UI構築
    # ------------------------------
    def _build_ui(self):
        # トップ情報
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        agg = self.aggregate_info
        info_text = (
            f"{self.display_name}   "
            f"製造期間: {agg.get('初回製造日', '-')} 〜 {agg.get('最終製造日', '-')}   "
            f"製造日数: {agg.get('製造日数', 0)}日   "
            f"ファイル数: {agg.get('ファイル数', 0)}   "
            f"総件数: {agg.get('総件数', 0):,}件"
        )
        ttk.Label(top, text=info_text, font=("", 10, "bold")).pack(side="left")

        self.status_var = tk.StringVar(value="データ読込中...")
        ttk.Label(top, textvariable=self.status_var,
                  foreground="navy").pack(side="right")

        # タブ
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.tab_summary = ttk.Frame(self.notebook)
        self.tab_trend = ttk.Frame(self.notebook)
        self.tab_daily = ttk.Frame(self.notebook)
        self.tab_export = ttk.Frame(self.notebook)

        self.notebook.add(self.tab_summary, text="📊 サマリー")
        self.notebook.add(self.tab_trend, text="📈 経時変化")
        self.notebook.add(self.tab_daily, text="📅 日別一覧")
        self.notebook.add(self.tab_export, text="💾 エクスポート")

        # 各タブの初期表示はローディング
        for tab in (self.tab_summary, self.tab_trend, self.tab_daily):
            ttk.Label(tab, text="データ読込中...",
                      font=("", 12), foreground="gray").pack(expand=True)

        # エクスポートタブだけは早めに作る
        self._build_export_tab()

    # ------------------------------
    # データ読込（バックグラウンド）
    # ------------------------------
    def _start_load_data(self):
        self.load_queue = queue.Queue()

        def worker():
            try:
                file_list = self.aggregate_info.get("_file_list", [])

                def progress_cb(current, total, fname):
                    self.load_queue.put(("progress", current, total, fname))

                combined_df, errors = load_hinshoku_data(
                    self.record_dir, self.hinshoku_num, file_list,
                    progress_callback=progress_cb,
                )
                daily_df = aggregate_by_date_folder(file_list)
                daily_df = detect_abnormal_dates(daily_df)

                if len(combined_df) > 0:
                    overall = compute_overall_stats(
                        combined_df,
                        reject_limit=self._reject_limit,
                        nominal_weight=self._nominal_weight,
                    )
                else:
                    overall = None

                self.load_queue.put(("done", {
                    "combined_df": combined_df,
                    "daily_df": daily_df,
                    "overall": overall,
                    "errors": errors,
                }))
            except Exception as e:
                import traceback
                self.load_queue.put(("error", f"{e}\n\n{traceback.format_exc()}"))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_load()

    def _poll_load(self):
        try:
            while True:
                msg = self.load_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, cur, total, fname = msg
                    short = fname if len(fname) < 60 else "..." + fname[-57:]
                    self.status_var.set(f"読込中: {cur}/{total}  {short}")
                elif kind == "done":
                    self._on_load_done(msg[1])
                    return
                elif kind == "error":
                    messagebox.showerror("読込エラー", msg[1], parent=self)
                    self.status_var.set("読込失敗")
                    return
        except queue.Empty:
            pass
        self.after(80, self._poll_load)

    def _on_load_done(self, result):
        self.combined_df = result["combined_df"]
        self.daily_df = result["daily_df"]
        self.overall_stats = result["overall"]
        errors = result["errors"]

        if len(self.combined_df) == 0:
            self.status_var.set("有効なデータがありません")
            messagebox.showwarning(
                "データなし",
                "この品種のデータが読み込めませんでした。",
                parent=self,
            )
            return

        n = len(self.combined_df)
        msg = f"読込完了: {n:,}レコード"
        if errors:
            msg += f" / 一部ファイル読込失敗 {len(errors)}件"
        self.status_var.set(msg)

        # 各タブの中身を構築
        self._build_summary_tab()
        self._build_trend_tab()
        self._build_daily_tab()
        self._enable_export_button()

    # ------------------------------
    # タブ1: サマリー
    # ------------------------------
    def _build_summary_tab(self):
        for w in self.tab_summary.winfo_children():
            w.destroy()

        # 左：統計値テーブル
        left = ttk.Frame(self.tab_summary, padding=10)
        left.pack(side="left", fill="y")

        ttk.Label(left, text="■ 統計値（全期間OKデータ）",
                  font=("", 10, "bold")).pack(anchor="w", pady=(0, 5))

        tree = ttk.Treeview(left, columns=("値",), show="tree headings", height=18)
        tree.column("#0", width=200, anchor="w")
        tree.column("値", width=130, anchor="e")
        tree.heading("#0", text="項目")
        tree.heading("値", text="値")

        s = self.overall_stats
        a = self.aggregate_info

        def _v(v, digits=3):
            return f"{v:.{digits}f}" if v is not None else "-"

        def _cap(v):
            if v is None:
                return "- (公称重量未登録)"
            mark = " ✓" if v >= 1.33 else (" △" if v >= 1.00 else " ✗")
            return f"{v:.3f}{mark}"

        rl = self._reject_limit
        nw_str = f"{self._nominal_weight:.3f} g" if self._nominal_weight is not None else "未登録"

        rows = [
            ("件数", f"{s['件数']:,}"),
            ("平均(g)", _v(s["平均"], 4)),
            ("σ(g)", _v(s["σ"], 5)),
            ("Min(g)", _v(s["Min"])),
            ("Max(g)", _v(s["Max"])),
            ("", ""),
            ("下限 −3σ (g)", _v(s["推奨下限"], 4)),
            ("上限 +3σ (g)", _v(s["推奨上限"], 4)),
            ("95%CI 下限(g)", _v(s["CI下限"], 4)),
            ("95%CI 上限(g)", _v(s["CI上限"], 4)),
            ("外れ値件数", f"{s['外れ値件数']:,}"),
            ("", ""),
            (f"工程実績性能  規格=±{rl}g / 公称={nw_str}", "≥1.33 推奨"),
            ("Pp",  _cap(s.get("Pp"))),
            ("Ppk", _cap(s.get("Ppk"))),
            ("", ""),
            ("総件数", f"{a.get('総件数', 0):,}"),
            ("不良率(%)", f"{a.get('不良率(%)', 0):.3f}"),
        ]
        for label, val in rows:
            tree.insert("", "end", text=label, values=(val,))
        tree.pack(fill="y")

        # 推奨規格値のメッセージ
        spec_frame = ttk.LabelFrame(left, text="推奨規格値（実績ベース）", padding=8)
        spec_frame.pack(fill="x", pady=(10, 0))
        spec_text = (
            f"平均 {s['平均']:.3f} g\n"
            f"範囲 {s['推奨下限']:.3f} 〜 {s['推奨上限']:.3f} g\n"
            f"幅 ±{3*s['σ']:.3f} g (3σ)"
        )
        ttk.Label(spec_frame, text=spec_text,
                  foreground="navy", font=("", 9)).pack(anchor="w")

        # 右：ヒストグラム
        right = ttk.Frame(self.tab_summary, padding=10)
        right.pack(side="right", fill="both", expand=True)

        fig = Figure(figsize=(7, 5), dpi=90)
        ax = fig.add_subplot(111)

        ok_data = pd.to_numeric(
            self.combined_df.loc[self.combined_df["ランクコード"] == "2", "測定値(g)"],
            errors="coerce"
        ).dropna()

        ax.hist(ok_data, bins=40, edgecolor="black", alpha=0.7, color="steelblue")
        ax.axvline(s["平均"], color="red", linestyle="-", linewidth=2,
                   label=f"平均: {s['平均']:.3f}")
        ax.axvline(s["推奨下限"], color="orange", linestyle="--", linewidth=2,
                   label=f"-3σ: {s['推奨下限']:.3f}")
        ax.axvline(s["推奨上限"], color="orange", linestyle="--", linewidth=2,
                   label=f"+3σ: {s['推奨上限']:.3f}")
        ax.set_title(f"{self.display_name} 全期間ヒストグラム(n={s['件数']:,})",
                     fontsize=11, fontweight="bold")
        ax.set_xlabel("測定値(g)")
        ax.set_ylabel("頻度")
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=right)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.draw()

    # ------------------------------
    # タブ2: 経時変化
    # ------------------------------
    def _build_trend_tab(self):
        for w in self.tab_trend.winfo_children():
            w.destroy()

        if self.daily_df is None or len(self.daily_df) == 0:
            ttk.Label(self.tab_trend, text="日別データがありません").pack(expand=True)
            return

        valid = self.daily_df.dropna(subset=["平均(g)"]).copy()
        if len(valid) == 0:
            ttk.Label(self.tab_trend, text="有効な日別データがありません").pack(expand=True)
            return

        valid["日付dt"] = pd.to_datetime(valid["日付"], format="%Y%m%d", errors="coerce")

        fig = Figure(figsize=(11, 7), dpi=90)
        gs = fig.add_gridspec(3, 1, hspace=0.15)
        ax1 = fig.add_subplot(gs[0])
        ax2 = fig.add_subplot(gs[1], sharex=ax1)
        ax3 = fig.add_subplot(gs[2], sharex=ax1)

        # 平均値
        ax1.plot(valid["日付dt"], valid["平均(g)"],
                 marker="o", color="steelblue", linewidth=1.5, markersize=5)
        overall_mean = valid["平均(g)"].mean()
        ax1.axhline(overall_mean, color="red", linestyle="--", alpha=0.5,
                    label=f"全期間平均: {overall_mean:.3f}")

        # 平均の異常日をハイライト
        reason_col = valid["異常理由"] if "異常理由" in valid.columns else pd.Series("", index=valid.index)
        mean_abnormal = valid[reason_col.str.contains("平均", na=False)]
        if len(mean_abnormal) > 0:
            ax1.scatter(mean_abnormal["日付dt"], mean_abnormal["平均(g)"],
                        color="red", s=80, zorder=5,
                        marker="o", facecolors="none", edgecolors="red", linewidth=2,
                        label=f"平均異常({len(mean_abnormal)}日)")

        ax1.set_ylabel("平均(g)", fontsize=10)
        ax1.set_title(f"{self.display_name} 日別推移",
                      fontsize=12, fontweight="bold")
        ax1.legend(fontsize=8)
        ax1.grid(True, alpha=0.3)
        plt.setp(ax1.xaxis.get_majorticklabels(), visible=False)

        # σ
        valid_std = valid.dropna(subset=["σ(g)"])
        ax2.plot(valid_std["日付dt"], valid_std["σ(g)"],
                 marker="s", color="darkorange", linewidth=1.5, markersize=5)

        # σの異常日をハイライト
        reason_col_std = valid_std["異常理由"] if "異常理由" in valid_std.columns else pd.Series("", index=valid_std.index)
        sigma_abnormal = valid_std[reason_col_std.str.contains("σ.*大", na=False)]
        if len(sigma_abnormal) > 0:
            ax2.scatter(sigma_abnormal["日付dt"], sigma_abnormal["σ(g)"],
                        color="red", s=80, zorder=5,
                        marker="o", facecolors="none", edgecolors="red", linewidth=2,
                        label=f"σ異常({len(sigma_abnormal)}日)")
            ax2.legend(fontsize=8)

        ax2.set_ylabel("σ(g)", fontsize=10)
        ax2.grid(True, alpha=0.3)
        plt.setp(ax2.xaxis.get_majorticklabels(), visible=False)

        # 不良率
        ax3.bar(valid["日付dt"], valid["不良率(%)"],
                color="firebrick", alpha=0.7, width=0.8)
        ax3.set_ylabel("不良率(%)", fontsize=10)
        ax3.set_xlabel("製造日", fontsize=10)
        ax3.grid(True, alpha=0.3)
        ax3.xaxis.set_major_formatter(mdates.DateFormatter("%Y/%m/%d"))
        plt.setp(ax3.xaxis.get_majorticklabels(), rotation=30, ha="right")

        canvas = FigureCanvasTkAgg(fig, master=self.tab_trend)
        canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=10)
        canvas.draw()

        # ナビゲーションツールバー(ズーム等)
        toolbar_frame = ttk.Frame(self.tab_trend)
        toolbar_frame.pack(fill="x", padx=10)
        NavigationToolbar2Tk(canvas, toolbar_frame)

    # ------------------------------
    # タブ3: 日別一覧
    # ------------------------------
    def _build_daily_tab(self):
        for w in self.tab_daily.winfo_children():
            w.destroy()

        if self.daily_df is None or len(self.daily_df) == 0:
            ttk.Label(self.tab_daily, text="日別データがありません").pack(expand=True)
            return

        hint_bar = ttk.Frame(self.tab_daily, padding=(10, 5, 10, 0))
        hint_bar.pack(fill="x")
        ttk.Label(hint_bar, text="💡 行をダブルクリックするとロット分割・分析Excelを作成できます",
                  foreground="gray").pack(side="right")

        frame = ttk.Frame(self.tab_daily, padding=10)
        frame.pack(fill="both", expand=True)

        cols = ("日付", "ファイル数", "総件数", "OK件数", "NG件数",
                "不良率(%)", "平均(g)", "σ(g)", "Min(g)", "Max(g)", "備考")
        self._daily_tree = ttk.Treeview(frame, columns=cols, show="headings", height=18)

        widths = {
            "日付": 90, "ファイル数": 70, "総件数": 75, "OK件数": 75, "NG件数": 70,
            "不良率(%)": 75, "平均(g)": 80, "σ(g)": 80, "Min(g)": 75, "Max(g)": 75,
            "備考": 250,
        }
        for col in cols:
            self._daily_tree.heading(col, text=col)
            anchor = "center" if col in ("日付", "ファイル数") else "e"
            if col == "備考":
                anchor = "w"
            self._daily_tree.column(col, anchor=anchor, width=widths[col])

        self._daily_tree.tag_configure("abnormal", background="#FFE4E1")

        for _, row in self.daily_df.iterrows():
            tags = ("abnormal",) if row.get("異常フラグ") else ()
            mean_v = row["平均(g)"]
            std_v = row["σ(g)"]
            min_v = row["Min(g)"]
            max_v = row["Max(g)"]
            self._daily_tree.insert("", "end", tags=tags, values=(
                row["日付"],
                row["ファイル数"],
                f"{row['総件数']:,}",
                f"{row['OK件数']:,}",
                f"{row['NG件数']:,}",
                f"{row['不良率(%)']:.3f}",
                f"{mean_v:.4f}" if pd.notna(mean_v) else "-",
                f"{std_v:.5f}"  if pd.notna(std_v)  else "-",
                f"{min_v:.3f}"  if pd.notna(min_v)  else "-",
                f"{max_v:.3f}"  if pd.notna(max_v)  else "-",
                row.get("異常理由", ""),
            ))

        self._daily_tree.bind("<Double-1>", self._on_analyze_day_activate)
        self._daily_tree.bind("<Return>", self._on_analyze_day_activate)

        sb = ttk.Scrollbar(frame, orient="vertical", command=self._daily_tree.yview)
        self._daily_tree.configure(yscrollcommand=sb.set)
        self._daily_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _on_analyze_day_activate(self, event=None):
        if not hasattr(self, "_daily_tree"):
            return
        selected = self._daily_tree.selection()
        if not selected:
            return
        values = self._daily_tree.item(selected[0], "values")
        if not values:
            return
        self._analyze_day(str(values[0]))

    def _analyze_day(self, date_folder):
        if self.combined_df is None or len(self.combined_df) == 0:
            messagebox.showwarning("データなし", "データが読込中です", parent=self)
            return

        try:
            target_date = pd.to_datetime(date_folder, format="%Y%m%d").date()
        except Exception:
            messagebox.showerror("エラー", f"日付の解析に失敗しました: {date_folder}", parent=self)
            return

        day_df = self.combined_df[
            self.combined_df["日付時刻"].dt.date == target_date
        ].copy()

        if len(day_df) == 0:
            messagebox.showwarning("データなし", f"{date_folder} のデータが見つかりません", parent=self)
            return

        dialog = LotPreviewDialog(self, day_df, self.hinshoku_num, product_name=self.product_name)
        self.wait_window(dialog)

        if dialog.result is None or dialog.result[0] == "cancel":
            return

        df_lots = dialog.result[1]

        save_dir = os.path.dirname(os.path.abspath(sys.argv[0]))

        created_lots = []
        skipped_lots = []
        for lot, group in df_lots.groupby("ロット"):
            total = len(group)
            status, ok_count = process_lot(group, lot, save_dir, self.hinshoku_num, product_name=self.product_name)
            if status == "ok":
                created_lots.append((lot, ok_count))
            else:
                skipped_lots.append((lot, ok_count, total))

        if not created_lots and skipped_lots:
            msg = "Excelファイルは作成されませんでした。\n\n"
            msg += "すべてのロットでOKデータが不足しています:\n"
            for lot, ok, total in skipped_lots:
                msg += f"  ・ロット{lot}: 総{total}件 / OK{ok}件\n"
            msg += "\nしきい値を変更するか、CSVの内容をご確認ください。"
            messagebox.showerror("作成失敗", msg, parent=self)
        elif skipped_lots:
            msg = f"Excel作成完了\n作成: {len(created_lots)}ファイル\n\n"
            msg += "⚠ 以下のロットはOKデータ不足のためスキップしました:\n"
            for lot, ok, total in skipped_lots:
                msg += f"  ・ロット{lot}: 総{total}件 / OK{ok}件\n"
            msg += f"\n（OKデータが {MIN_OK_COUNT} 件未満のロットは統計計算ができません）"
            messagebox.showwarning("完了（一部スキップ）", msg, parent=self)
        else:
            messagebox.showinfo(
                "完了",
                f"Excel作成完了\n{len(created_lots)}ファイルを作成しました。",
                parent=self,
            )

    # ------------------------------
    # タブ4: エクスポート
    # ------------------------------
    def _build_export_tab(self):
        frame = ttk.Frame(self.tab_export, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text=f"{self.display_name} の詳細レポートをExcelに出力します。",
            font=("", 11),
        ).pack(anchor="w", pady=(0, 10))

        ttk.Label(
            frame,
            text=(
                "出力内容:\n"
                "  ・統計結果（全期間サマリー）\n"
                "  ・日別集計\n"
                "  ・ヒストグラム\n"
                "  ・時系列チャート\n"
                "  ・日別推移グラフ\n"
                "  ・全OKデータ（生データ）\n"
                "  ・外れ値"
            ),
            justify="left",
        ).pack(anchor="w", pady=(0, 20))

        self.export_btn = ttk.Button(
            frame,
            text="📤 Excelに出力する",
            command=self._on_export,
            state="disabled",
        )
        self.export_btn.pack(anchor="w")

        self.export_status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.export_status_var,
                  foreground="navy").pack(anchor="w", pady=(10, 0))

    def _enable_export_button(self):
        if hasattr(self, "export_btn"):
            self.export_btn.config(state="normal")

    def _on_export(self):
        if self.combined_df is None or self.overall_stats is None:
            messagebox.showwarning("データなし", "出力するデータがありません",
                                   parent=self)
            return

        safe_name = self.display_name.replace("/", "-").replace("\\", "-")
        default_name = (
            f"品種詳細_{safe_name}_"
            f"{datetime.datetime.now():%Y%m%d_%H%M}.xlsx"
        )
        filepath = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".xlsx",
            initialfile=default_name,
            initialdir=self.record_dir,
            filetypes=[("Excel files", "*.xlsx")],
        )
        if not filepath:
            return

        try:
            self.export_status_var.set("出力中...")
            self.update_idletasks()

            export_hinshoku_detail(
                filepath=filepath,
                hinshoku_num=self.hinshoku_num,
                aggregate_info=self.aggregate_info,
                combined_df=self.combined_df,
                daily_df=self.daily_df,
                overall_stats=self.overall_stats,
                product_name=self.product_name,
            )

            self.export_status_var.set(f"✓ 出力完了: {os.path.basename(filepath)}")
            messagebox.showinfo(
                "完了",
                f"Excelファイルを出力しました:\n{filepath}",
                parent=self,
            )
        except Exception as e:
            import traceback
            self.export_status_var.set("出力失敗")
            messagebox.showerror(
                "エラー",
                f"出力に失敗しました:\n{e}\n\n{traceback.format_exc()}",
                parent=self,
            )
