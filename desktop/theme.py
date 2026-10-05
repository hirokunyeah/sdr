"""
アプリの見た目（ダークテーマ）。
"""
import os

from PySide6 import QtCore, QtGui, QtWidgets

C = {
    "bg": "#0f1115", "panel": "#181b22", "field": "#0f1218", "raised": "#222734",
    "line": "#2a2f3a", "text": "#e6e8ee", "muted": "#8a93a6", "dim": "#555c6b",
    "accent": "#4fc3f7", "accent_dim": "#1e3a4a", "primary": "#1e6f9f",
    "marker": "#ff5252", "rec": "#e5534b", "rec_dim": "#7a2b2b", "ok": "#5fd38d",
    "plot": "#0b0d11", "grid": "#1f2430",
}

UI_FONTS = ["BIZ UDPGothic", "BIZ UDPゴシック", "Yu Gothic UI", "Hiragino Sans", "Noto Sans CJK JP"]
MONO_FONTS = ["BIZ UDGothic", "BIZ UDゴシック", "Consolas", "Menlo", "Noto Sans Mono CJK JP", "monospace"]

STYLE = """
QWidget {{ background: {bg}; color: {text}; }}
QLabel, QCheckBox, QRadioButton {{ background: transparent; }}
QLabel[role="caption"] {{ color: {muted}; font-size: 9pt; }}
QLabel[role="heading"] {{ color: {muted}; font-weight: bold; }}
QLabel[role="hint"] {{ color: {dim}; }}

QFrame#Panel {{ background: {panel}; border: 1px solid {line}; border-radius: 10px; }}
QFrame#Panel QWidget {{ background: transparent; }}

QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit {{
    background: {field}; border: 1px solid {line}; border-radius: 6px;
    padding: 4px 8px; min-height: 22px; selection-background-color: {accent_dim};
}}
QFrame#Panel QComboBox, QFrame#Panel QDoubleSpinBox, QFrame#Panel QLineEdit {{ background: {field}; }}
QComboBox:hover, QDoubleSpinBox:hover, QSpinBox:hover, QLineEdit:hover {{ border-color: #3a4152; }}
QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus {{ border-color: {accent}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {panel}; border: 1px solid {line}; outline: none;
    selection-background-color: {accent_dim}; selection-color: {text};
}}

QDoubleSpinBox#FreqSpin, QFrame#Panel QDoubleSpinBox#FreqSpin {{
    background: transparent; border: none; color: {accent};
    padding: 0; min-height: 46px; selection-background-color: {accent_dim};
}}

QPushButton {{
    background: {raised}; border: 1px solid {line}; border-radius: 6px; padding: 6px 12px;
}}
QPushButton:hover {{ border-color: {accent}; }}
QPushButton:pressed {{ background: {field}; }}
QPushButton:disabled {{ color: {dim}; border-color: {line}; }}
QFrame#Panel QPushButton {{ background: {raised}; }}
QPushButton[kind="primary"], QFrame#Panel QPushButton[kind="primary"] {{
    background: {primary}; border-color: {primary}; font-weight: bold; padding: 8px 18px;
}}
QPushButton[kind="primary"]:hover {{ border-color: {accent}; }}
QPushButton[kind="primary"]:checked, QFrame#Panel QPushButton[kind="primary"]:checked {{
    background: {rec_dim}; border-color: {rec}; }}
QPushButton[kind="danger"] {{ color: #ff8a80; border-color: {rec_dim}; }}
QPushButton[kind="danger"]:hover {{ border-color: {rec}; }}
QPushButton[kind="danger"][active="true"] {{ background: {rec_dim}; color: {text}; border-color: {rec}; }}
QPushButton[kind="step"] {{ padding: 2px 0; min-width: 30px; max-width: 30px; font-size: 13pt; }}

QPushButton[seg] {{ border-radius: 0; padding: 6px 14px; margin: 0; }}
QPushButton[seg="first"] {{ border-top-left-radius: 6px; border-bottom-left-radius: 6px; }}
QPushButton[seg="last"] {{ border-top-right-radius: 6px; border-bottom-right-radius: 6px; }}
QPushButton[seg]:checked, QFrame#Panel QPushButton[seg]:checked {{
    background: {accent}; border-color: {accent}; color: #071018; font-weight: bold; }}

QListWidget, QTableWidget, QPlainTextEdit {{
    background: {field}; border: 1px solid {line}; border-radius: 8px; outline: none;
    alternate-background-color: #131722;
}}
QListWidget::item {{ padding: 6px 8px; border-radius: 4px; }}
QListWidget::item:hover {{ background: #161b26; }}
QListWidget::item:selected, QTableWidget::item:selected {{ background: {accent_dim}; color: {text}; }}
QTableWidget {{ gridline-color: transparent; }}
QTableWidget::item {{ padding: 4px 6px; }}
QHeaderView::section {{
    background: {panel}; color: {muted}; border: none; border-bottom: 1px solid {line};
    padding: 6px; font-weight: bold;
}}

QGroupBox {{
    background: {panel}; border: 1px solid {line}; border-radius: 10px;
    margin-top: 18px; padding: 8px 10px 10px 10px;
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left; left: 10px; padding: 0 4px;
    color: {muted}; font-weight: bold;
}}
QGroupBox QWidget {{ background: transparent; }}
QGroupBox QComboBox, QGroupBox QDoubleSpinBox, QGroupBox QLineEdit,
QGroupBox QListWidget, QGroupBox QTableWidget, QGroupBox QPlainTextEdit {{ background: {field}; }}
QGroupBox QPushButton {{ background: {raised}; }}
QGroupBox QHeaderView::section {{ background: {panel}; }}

QLabel#StatusCard {{
    background: {panel}; border: 1px solid {line}; border-left: 4px solid {accent};
    border-radius: 8px; padding: 10px 12px; font-size: 11pt; font-weight: bold;
}}
QLabel#StatusCard[tone="rec"] {{ border-left-color: {rec}; color: #ff8a80; }}
QLabel#StatusCard[tone="busy"] {{ border-left-color: #f0b429; }}
QLabel#StatusCard[tone="idle"] {{ border-left-color: {dim}; color: {muted}; }}

QLabel#ImageView {{ background: {plot}; border: 1px solid {line}; border-radius: 8px; color: {dim}; }}

QSplitter::handle {{ background: {bg}; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QSplitter::handle:vertical {{ height: 6px; }}

QStatusBar {{ background: {panel}; color: {muted}; border-top: 1px solid {line}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ padding: 0 6px; }}

QProgressBar {{
    background: {field}; border: 1px solid {line}; border-radius: 5px;
    text-align: center; color: {text}; max-height: 14px;
}}
QProgressBar::chunk {{ background: {accent}; border-radius: 4px; }}

QSlider::groove:horizontal {{ height: 4px; background: {line}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {text}; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: {accent}; }}

QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border: 1px solid #3a4152; border-radius: 4px; background: {field};
}}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; image: url({check}); }}
QCheckBox::indicator:hover {{ border-color: {accent}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: #2f3543; border-radius: 3px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: #3d4556; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QToolTip {{ background: {panel}; color: {text}; border: 1px solid {line}; padding: 4px 6px; }}
QMessageBox, QFileDialog {{ background: {bg}; }}
""".format(check=os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons", "check.svg").replace("\\", "/"),
           **C)


def font(families, size=None, bold=False):
    f = QtGui.QFont()
    available = set(QtGui.QFontDatabase.families())
    f.setFamilies([n for n in families if n in available] or families)
    if size:
        f.setPointSizeF(size)
    f.setBold(bold)
    return f


def apply(app):
    """QApplication にダークテーマを適用する"""
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    for role, key in ((QtGui.QPalette.Window, "bg"), (QtGui.QPalette.Base, "field"),
                      (QtGui.QPalette.AlternateBase, "panel"), (QtGui.QPalette.Button, "raised"),
                      (QtGui.QPalette.Text, "text"), (QtGui.QPalette.WindowText, "text"),
                      (QtGui.QPalette.ButtonText, "text"), (QtGui.QPalette.ToolTipBase, "panel"),
                      (QtGui.QPalette.ToolTipText, "text"), (QtGui.QPalette.Highlight, "accent_dim"),
                      (QtGui.QPalette.HighlightedText, "text"), (QtGui.QPalette.PlaceholderText, "dim")):
        pal.setColor(role, QtGui.QColor(C[key]))
    pal.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Text, QtGui.QColor(C["dim"]))
    pal.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.ButtonText, QtGui.QColor(C["dim"]))
    app.setPalette(pal)
    app.setFont(font(UI_FONTS, 10))
    app.setStyleSheet(STYLE)


def panel():
    """角丸の背景パネル（QFrame#Panel）"""
    f = QtWidgets.QFrame()
    f.setObjectName("Panel")
    return f


def caption(text, role="caption"):
    lab = QtWidgets.QLabel(text)
    lab.setProperty("role", role)
    return lab


def field(text, widget):
    """上にキャプションを付けた入力欄"""
    w = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addWidget(caption(text))
    if isinstance(widget, QtWidgets.QLayout):
        lay.addLayout(widget)
    else:
        lay.addWidget(widget)
    return w


def set_prop(widget, name, value):
    """動的プロパティを変えてスタイルを再適用する"""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class Segmented(QtWidgets.QWidget):
    """横並びの切り替えボタン。QComboBox と同じ currentText / setCurrentText / currentIndexChanged を持つ"""
    currentIndexChanged = QtCore.Signal(int)

    def __init__(self, items, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.group = QtWidgets.QButtonGroup(self)
        self.group.setExclusive(True)
        for i, text in enumerate(items):
            b = QtWidgets.QPushButton(text)
            b.setCheckable(True)
            b.setProperty("seg", "first" if i == 0 else "last" if i == len(items) - 1 else "mid")
            self.group.addButton(b, i)
            lay.addWidget(b)
        self.group.button(0).setChecked(True)
        self.group.idToggled.connect(lambda i, on: on and self.currentIndexChanged.emit(i))

    def currentText(self):
        return self.group.checkedButton().text()

    def currentIndex(self):
        return self.group.checkedId()

    def setCurrentText(self, text):
        for b in self.group.buttons():
            if b.text() == text:
                b.setChecked(True)
