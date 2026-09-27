import sys

from PySide6.QtWidgets import QApplication

app = QApplication([])
QApplication.primaryScreen().grabWindow(0).save(sys.argv[1])
