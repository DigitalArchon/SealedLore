"""Settings → Lore, the built-in embeddings model (providers/local_embeddings.py):
whether it is on this computer, downloading it once, and taking it off.

The download runs on a counted background thread (gui/workers.run_detached),
its progress posted to the GUI thread; Stop ends it between pieces and keeps
nothing half-made. A Settings dialog closed meanwhile lets it finish."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from sealedlore.gui.workers import post, run_detached
from sealedlore.providers.local_embeddings import (
    DOWNLOAD_BYTES,
    download,
    installed,
    model_dir,
    remove,
    size_words,
)

NOT_YET = (
    "Not on this computer yet: {size}, downloaded once from Hugging Face and checked "
    "against its known checksum. Until it is, lore is matched by keywords."
)
HERE = "On this computer ({size}). Nothing you write is sent anywhere to embed it."
WHAT = (
    "bge-m3, the model lore and recall are tuned on, run by ONNX Runtime on this "
    "computer. It retrieved lore as well as NanoGPT's in our tests; the first use "
    "loads it (about 0.6 GB of memory)."
)


class BuiltinModelRow(QWidget):
    """The built-in model's state, with Download… / Stop and Remove."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self.root = root
        self.setObjectName("builtinEmbeddings")
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        about = QLabel(WHAT)
        about.setObjectName("hintLabel")
        about.setWordWrap(True)
        self.state = QLabel()
        self.state.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        self.download_button = QPushButton("Download…")
        self.download_button.clicked.connect(self._download_or_stop)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)
        row = QHBoxLayout()
        row.addWidget(self.state, 1)
        row.addWidget(self.download_button)
        row.addWidget(self.remove_button)
        column.addWidget(about)
        column.addLayout(row)
        column.addWidget(self.bar)
        self._stopping = False
        self._running = False
        self.refresh()

    @property
    def downloading(self) -> bool:
        return self._running

    def refresh(self, said: str = "") -> None:
        here = installed(self.root)
        size = size_words(DOWNLOAD_BYTES)
        self.state.setText(said or (HERE if here else NOT_YET).format(size=size))
        self.state.setToolTip(str(model_dir(self.root)))
        self.download_button.setVisible(not here or self._running)
        self.download_button.setText("Stop" if self._running else "Download…")
        self.remove_button.setVisible(here and not self._running)
        self.bar.setVisible(self._running)

    def _download_or_stop(self) -> None:
        if self._running:
            self._stopping = True
            self.download_button.setEnabled(False)
            return
        self._running, self._stopping = True, False
        self.bar.setValue(0)
        self.refresh("Downloading…")
        root = self.root

        def job() -> Path:
            return download(
                root,
                progress=lambda done, total: post(self, self._progress, done, total),
                stopped=lambda: self._stopping,
            )

        run_detached(self, job, self._done, name="embeddings-model")

    def _progress(self, done: int, total: int) -> None:
        if total:
            self.bar.setValue(int(1000 * done / total))
            self.state.setText(f"Downloading… {size_words(done)} of {size_words(total)}")

    def _done(self, _result: object, error: str | None) -> None:
        self._running = False
        self.download_button.setEnabled(True)
        if error is None:
            self.refresh()
        elif self._stopping:
            self.refresh(
                "Stopped: nothing half-downloaded was kept. "
                + NOT_YET.format(size=size_words(DOWNLOAD_BYTES))
            )
        else:
            self.refresh(f"⚠ {error}")

    def _remove(self) -> None:
        remove(self.root)
        self.refresh()
