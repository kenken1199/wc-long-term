"""matplotlib の日本語フォント設定を行うモジュール。"""

import platform

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt

_FONT_CANDIDATES = {
    "Windows": ["MS Gothic", "Yu Gothic", "Meiryo"],
    "Darwin":  ["Hiragino Sans", "Hiragino Maru Gothic Pro", "AppleGothic"],
    "Linux":   ["Noto Sans CJK JP", "IPAGothic", "IPAPGothic"],
}


def setup_japanese_font():
    candidates = _FONT_CANDIDATES.get(platform.system(), [])
    available = {f.name for f in fm.fontManager.ttflist}
    for font in candidates:
        if font in available:
            plt.rcParams["font.family"] = font
            return


setup_japanese_font()
