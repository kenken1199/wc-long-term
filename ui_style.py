"""
==========================================================
 UI共通スタイル（ライト・フラットテーマ）
==========================================================
 analyze.py / record_analyzer.py / record_dialog.py / lot_analyze.py
 すべてのウィンドウ・ダイアログで共通の配色・ttkスタイルを提供する。
==========================================================
"""

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

# --- 配色パレット ---
APP_BG      = "#F7F8FA"   # ウィンドウ全体の背景
CARD_BG     = "#FFFFFF"   # カード/パネル/入力欄の背景
APP_TEXT    = "#1F2933"   # 標準テキスト
APP_SUBTEXT = "#6B7280"   # 補助テキスト（グレー）
APP_BORDER  = "#E4E7EB"   # 罫線・区切り線
HEADER_BG   = "#EDEFF2"   # テーブル見出し・非選択タブの背景

APP_ACCENT        = "#3B82F6"   # アクセント（青）
APP_ACCENT_ACTIVE = "#2563EB"   # アクセント（押下時）
APP_ACCENT_TEXT   = "#FFFFFF"

ROW_BASE = "#FFFFFF"   # Treeview 偶数行
ROW_ALT  = "#F3F4F6"   # Treeview 奇数行（縞模様）

WARN_BG   = "#FEF3C7"   # 注意（スキップ予定 等）
WARN_TEXT = "#92400E"

ERROR_BG   = "#FEE2E2"   # NG・異常値・外れ値
ERROR_TEXT = "#B91C1C"

_FONT_CANDIDATES = ["Yu Gothic UI", "Meiryo UI", "Yu Gothic", "Meiryo", "MS Gothic"]


def _pick_font_family(root):
    try:
        available = set(tkfont.families(root))
    except tk.TclError:
        return "TkDefaultFont"
    for name in _FONT_CANDIDATES:
        if name in available:
            return name
    return "TkDefaultFont"


def apply_style(root):
    """ttk.Style にライト・フラットテーマを適用する。

    root: tk.Tk または既存の Tk インタプリタに紐づく任意のウィジェット。
    戻り値の ttk.Style はプロセス全体（同一Tclインタプリタ）に適用されるため、
    複数のTopLevelダイアログを開いても再設定は不要。
    """
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    family = _pick_font_family(root)
    base_font = (family, 10)
    bold_font = (family, 10, "bold")

    style.configure(".", background=APP_BG, foreground=APP_TEXT, font=base_font)
    style.configure("TFrame", background=APP_BG)
    style.configure("TLabel", background=APP_BG, foreground=APP_TEXT)
    style.configure("TLabelframe", background=APP_BG, foreground=APP_TEXT,
                     bordercolor=APP_BORDER, relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=APP_BG, foreground=APP_TEXT, font=bold_font)

    style.configure(
        "TButton",
        background=CARD_BG, foreground=APP_TEXT,
        bordercolor=APP_BORDER, padding=7, relief="flat", font=base_font,
    )
    style.map(
        "TButton",
        background=[("active", "#EEF1F5"), ("pressed", HEADER_BG)],
    )

    style.configure(
        "Accent.TButton",
        background=APP_ACCENT, foreground=APP_ACCENT_TEXT,
        padding=8, font=bold_font, borderwidth=0, relief="flat",
    )
    style.map(
        "Accent.TButton",
        background=[("active", APP_ACCENT_ACTIVE), ("pressed", APP_ACCENT_ACTIVE)],
        foreground=[("active", APP_ACCENT_TEXT), ("pressed", APP_ACCENT_TEXT)],
    )

    style.configure(
        "Toolbutton",
        background=CARD_BG, foreground=APP_TEXT, padding=6, relief="flat",
    )
    style.map(
        "Toolbutton",
        background=[("selected", APP_ACCENT), ("active", "#EEF1F5")],
        foreground=[("selected", APP_ACCENT_TEXT)],
    )

    style.configure(
        "Treeview",
        background=CARD_BG, fieldbackground=CARD_BG, foreground=APP_TEXT,
        rowheight=26, borderwidth=0, font=base_font,
    )
    style.configure(
        "Treeview.Heading",
        background=HEADER_BG, foreground=APP_TEXT,
        font=bold_font, relief="flat", padding=6,
    )
    style.map("Treeview.Heading", background=[("active", APP_BORDER)])
    style.map(
        "Treeview",
        background=[("selected", APP_ACCENT)],
        foreground=[("selected", APP_ACCENT_TEXT)],
    )

    style.configure("TNotebook", background=APP_BG, borderwidth=0, tabmargins=(2, 4, 2, 0))
    style.configure(
        "TNotebook.Tab",
        background=HEADER_BG, foreground=APP_SUBTEXT,
        padding=(14, 7), font=base_font, borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", CARD_BG)],
        foreground=[("selected", APP_TEXT)],
        font=[("selected", bold_font)],
    )

    style.configure("TEntry", fieldbackground=CARD_BG, bordercolor=APP_BORDER, padding=4)
    style.configure("TSpinbox", fieldbackground=CARD_BG, bordercolor=APP_BORDER, padding=4)
    style.configure("TCombobox", fieldbackground=CARD_BG, bordercolor=APP_BORDER)

    style.configure(
        "TScrollbar",
        background=HEADER_BG, troughcolor=APP_BG, bordercolor=APP_BG, arrowcolor=APP_SUBTEXT,
    )
    style.configure("TProgressbar", background=APP_ACCENT, troughcolor=HEADER_BG, bordercolor=APP_BG)
    style.configure("TRadiobutton", background=APP_BG, foreground=APP_TEXT, font=base_font)
    style.configure("TCheckbutton", background=APP_BG, foreground=APP_TEXT, font=base_font)

    return style


def style_toplevel(window):
    """tk.Tk / tk.Toplevel の素のウィジェット背景をテーマ背景色に合わせる。"""
    window.configure(bg=APP_BG)


def stripe_treeview(tree, tag_odd="oddrow", tag_even="evenrow"):
    """Treeview に縞模様（ゼブラストライプ）のタグを登録する。"""
    tree.tag_configure(tag_odd, background=ROW_ALT)
    tree.tag_configure(tag_even, background=ROW_BASE)


def stripe_tag(index, tag_odd="oddrow", tag_even="evenrow"):
    """行インデックスから縞模様タグを返す。他のタグと併用する場合は
    tags=(other_tag, stripe_tag(i)) のようにタプルへ含める。"""
    return tag_odd if index % 2 == 1 else tag_even
