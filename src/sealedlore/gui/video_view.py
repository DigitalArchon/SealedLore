"""Videos in the transcript: a still until played, played in place.

Memory and speed come first (the author: AppImage size doesn't matter,
performance and RAM do):

- A video shows its first frame (the poster, a JPEG kept beside it), held at
  the size it is shown, as a picture is. Nothing is decoded until Play.
- There is one player for the whole window (`playback()`): playing a video
  stops any other. It draws frames from a `QVideoSink` onto the video's own
  widget, never a `QVideoWidget`, which in Qt 6 renders through a native
  window of its own that misbehaves in a scrolling column, over the
  background picture, and on Wayland.
- A video on disk is played from its file (streamed, not read into memory);
  a memory-only chat's from its bytes, through a `QBuffer`.
- QtMultimedia (and FFmpeg under it) loads only when the first video is
  played or its frames are taken, never at startup.

`FrameGrabber` takes a new video's first and last frames once, when it
arrives: the poster, and the picture offered as the start of the next video.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.ref_images import pixmap_within
from sealedlore.models.video import GeneratedVideo

# The largest a video is shown in the column (as a picture's height cap).
VIDEO_MAX_HEIGHT = 520
# How long taking a video's frames may take before it is given up.
GRAB_TIMEOUT_MS = 20_000


@dataclass(frozen=True)
class VideoItem:
    """A video for the transcript: its record, its poster's bytes, and how
    to reach its file (a path on disk, else its bytes)."""

    record: GeneratedVideo
    poster: bytes | None
    source: Callable[[], Path | bytes | None]
    # A job is asking after it now (else it can be asked after again).
    running: bool = False
    can_open: bool = True


# Qt's FFmpeg prints each file it opens to stderr (its path, its tags): not
# wanted from a story's videos, nor from a memory-only chat's. The
# environment's QT_LOGGING_RULES still wins, for debugging.
_QUIET = "qt.multimedia.ffmpeg*=false\nqt.multimedia.playbackengine*=false"
_quietened = False


def multimedia_available() -> bool:
    global _quietened
    if not _quietened:
        from PySide6.QtCore import QLoggingCategory

        QLoggingCategory.setFilterRules(_QUIET)
        _quietened = True
    try:
        import PySide6.QtMultimedia  # noqa: F401
    except ImportError:
        return False
    return True


def _set_source(player, source: Path | bytes, owner: QObject) -> QBuffer | None:
    """Point `player` at a file, or at bytes through a buffer it keeps."""
    if isinstance(source, Path):
        player.setSource(QUrl.fromLocalFile(str(source)))
        return None
    buffer = QBuffer(owner)
    buffer.setData(QByteArray(source))
    buffer.open(QIODevice.ReadOnly)
    player.setSourceDevice(buffer, QUrl("video.mp4"))
    return buffer


def image_jpeg(image: QImage, quality: int = 88) -> bytes:
    """A frame as JPEG bytes, with no text blocks (Qt adds none to JPEG)."""
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "JPEG", quality)
    buffer.close()
    return bytes(data)


class FrameGrabber(QObject):
    """A video's first and last frames and its length, taken once.

    `done(first, last, seconds)`: JPEG bytes (empty when a frame couldn't
    be had) and the length in seconds (0 if unknown). Runs on the GUI
    thread; the decoding is Qt's own threads'."""

    done = Signal(bytes, bytes, float)

    def __init__(self, source: Path | bytes, parent: QObject | None = None) -> None:
        super().__init__(parent)
        multimedia_available()
        from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

        self._player = QMediaPlayer(self)
        self._sink = QVideoSink(self)
        self._player.setVideoSink(self._sink)
        self._first: QImage | None = None
        self._last: QImage | None = None
        self._seeking = False
        self._finished = False
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(lambda *_args: self._finish())
        self._buffer = _set_source(self._player, source, self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._finish)
        self._timer.start(GRAB_TIMEOUT_MS)

    def _on_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if status == QMediaPlayer.MediaStatus.LoadedMedia and self._first is None:
            self._player.pause()  # decodes and shows the first frame
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._finish()

    def _on_frame(self, frame) -> None:
        if self._finished or not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        if self._first is None:
            self._first = image
            self._seeking = True
            duration = self._player.duration()
            self._player.setPosition(max(0, duration - 60))
            if duration <= 0:
                self._finish()
        elif self._seeking and self._player.position() > 0:
            self._last = image
            self._finish()

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._timer.stop()
        seconds = max(0.0, self._player.duration() / 1000)
        self._player.stop()
        first = image_jpeg(self._first) if self._first is not None else b""
        last = image_jpeg(self._last) if self._last is not None else first
        self.done.emit(first, last, seconds)


class Playback(QObject):
    """The window's one player. `play(surface, source)` plays into a
    surface, stopping whatever else was playing."""

    def __init__(self) -> None:
        super().__init__()
        multimedia_available()
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)
        self._surface: VideoSurface | None = None
        self._buffer: QBuffer | None = None

    @property
    def surface(self) -> VideoSurface | None:
        return self._surface

    def play(self, surface: VideoSurface, source: Path | bytes) -> None:
        if self._surface is surface:
            self.player.play()
            return
        self.release()
        self._surface = surface
        surface.destroyed.connect(self._forget)
        self._buffer = _set_source(self.player, source, self)
        self.audio.setMuted(surface.muted)
        self.player.play()

    def toggle(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def release(self, surface: VideoSurface | None = None) -> None:
        """Stop, and let go of the video (and its bytes)."""
        if surface is not None and surface is not self._surface:
            return
        self.player.stop()
        self.player.setSource(QUrl())
        if self._buffer is not None:
            self._buffer.close()
            self._buffer.deleteLater()
            self._buffer = None
        previous, self._surface = self._surface, None
        if previous is not None:
            try:
                previous.destroyed.disconnect(self._forget)
            except (RuntimeError, TypeError):
                pass
            previous.stopped()

    def _forget(self, *_args) -> None:
        self._surface = None
        self.player.stop()
        self.player.setSource(QUrl())

    def _on_frame(self, frame) -> None:
        if self._surface is not None and frame.isValid():
            self._surface.show_frame(frame.toImage())

    def _on_position(self, position: int) -> None:
        if self._surface is not None:
            self._surface.show_position(position, self.player.duration())

    def _on_state(self, state) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if self._surface is not None:
            self._surface.show_playing(state == QMediaPlayer.PlaybackState.PlayingState)

    def _on_error(self, _error, message: str) -> None:
        if self._surface is not None:
            self._surface.show_error(message or "The video couldn't be played.")


_playback: Playback | None = None


def playback() -> Playback:
    global _playback
    if _playback is None:
        _playback = Playback()
    return _playback


def stop_playback() -> None:
    """Before a rebuild or the window closes: nothing plays into a widget
    that is going."""
    if _playback is not None:
        _playback.release()


class VideoSurface(QWidget):
    """The poster, or the frame playing, scaled to the column's width."""

    clicked = Signal()

    def __init__(self, poster: QPixmap, aspect: QSize) -> None:
        super().__init__()
        self._poster = poster
        self._aspect = aspect if not aspect.isEmpty() else QSize(16, 9)
        self._frame: QImage | None = None
        self._playing = False
        self.muted = False
        policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setCursor(Qt.PointingHandCursor)
        # Called back by the player, which holds none of this widget's state.
        self.on_position: Callable[[int, int], None] | None = None
        self.on_playing: Callable[[bool], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        self.on_stopped: Callable[[], None] | None = None

    def _size_for(self, width: int) -> QSize:
        if width <= 0:
            return QSize(0, 0)
        return self._aspect.scaled(width, VIDEO_MAX_HEIGHT, Qt.KeepAspectRatio)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        return self._size_for(width).height()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        width = self.width() or 480
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(0, self.heightForWidth(self.width()) if self.width() else 0)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt naming
        target = self._size_for(self.width())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.fillRect(0, 0, target.width(), target.height(), QColor(0, 0, 0))
        if self._frame is not None:
            painter.drawImage(self._fitted(self._frame.size(), target), self._frame)
        elif not self._poster.isNull():
            painter.drawPixmap(self._fitted(self._poster.size(), target), self._poster)
        if not self._playing:
            self._draw_play(painter, target)

    @staticmethod
    def _fitted(size: QSize, target: QSize):
        from PySide6.QtCore import QRect

        scaled = size.scaled(target, Qt.KeepAspectRatio)
        x = (target.width() - scaled.width()) // 2
        y = (target.height() - scaled.height()) // 2
        return QRect(x, y, scaled.width(), scaled.height())

    @staticmethod
    def _draw_play(painter: QPainter, target: QSize) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPolygonF

        radius = max(18, min(target.width(), target.height()) // 10)
        centre = QPointF(target.width() / 2, target.height() / 2)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.drawEllipse(centre, radius, radius)
        painter.setBrush(QColor(255, 255, 255, 230))
        side = radius * 0.9
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(centre.x() - side * 0.35, centre.y() - side * 0.5),
                    QPointF(centre.x() - side * 0.35, centre.y() + side * 0.5),
                    QPointF(centre.x() + side * 0.55, centre.y()),
                ]
            )
        )

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    # -- called by the player --------------------------------------------------

    def show_frame(self, image: QImage) -> None:
        # Kept at the size shown: a 1080p frame is 8 MB, the column's a third.
        target = self._size_for(self.width())
        if not target.isEmpty() and image.width() > target.width():
            image = image.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._frame = image
        self.update()

    def show_position(self, position: int, duration: int) -> None:
        if self.on_position is not None:
            self.on_position(position, duration)

    def show_playing(self, playing: bool) -> None:
        self._playing = playing
        self.update()
        if self.on_playing is not None:
            self.on_playing(playing)

    def show_error(self, message: str) -> None:
        if self.on_error is not None:
            self.on_error(message)

    def stopped(self) -> None:
        self._playing = False
        self._frame = None
        self.update()
        if self.on_stopped is not None:
            self.on_stopped()


def video_caption(video: GeneratedVideo) -> str:
    parts = ["Video", video.model.rsplit("/", 1)[-1]]
    resolution = video.settings.get("resolution")
    if resolution:
        parts.append(str(resolution))
    if video.seconds:
        parts.append(video.seconds)
    paid = video.cost if video.cost is not None else video.charged
    if paid is not None:
        parts.append(
            f"{'' if video.cost_reported or video.charged is not None else '~'}${paid:.2f}"
        )
    return "  ·  ".join(parts)


VIDEO_ACTIONS = (
    ("play", "Play"),
    ("open", "Open in the default player"),
    ("prompt", "Show the prompt"),
    ("save", "Save video as…"),
    ("-", ""),
    ("continue", "Continue from its last frame…"),
    ("frame", "Use its last frame as a reference picture for…"),
    ("again", "Generate again…"),
    ("-", ""),
    ("delete", "Delete video"),
)


def _clock(ms: int) -> str:
    seconds = max(0, ms // 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


class VideoMessageWidget(QFrame):
    """A video, after the passage it shows. Like a picture it isn't the
    story: nothing here is ever sent to the storyteller."""

    # (action, video id): play, open, prompt, save, continue, frame, again, delete.
    action_requested = Signal(str, str)

    def __init__(self, item: VideoItem) -> None:
        super().__init__()
        video = item.record
        self.setObjectName("message")
        self.setProperty("kind", "picture")
        self.video_id = video.id
        self._source = item.source

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)
        self.header = QLabel(video_caption(video))
        self.header.setObjectName("messageHeader")
        self.header.setToolTip(video.prompt)
        menu = QMenu(self)
        for key, text in VIDEO_ACTIONS:
            if key == "-":
                menu.addSeparator()
                continue
            if key == "open" and not item.can_open:
                continue  # a memory-only chat's video has no file to open
            if key in ("continue", "frame") and not video.last_frame:
                continue
            action = menu.addAction(text)
            action.triggered.connect(lambda _checked=False, k=key: self._act(k))
        more = QToolButton()
        more.setObjectName("messageAction")
        more.setText("⋯")
        more.setToolTip("Play, open, save, continue from it, generate again")
        more.setPopupMode(QToolButton.InstantPopup)
        more.setMenu(menu)
        self.more_button = more
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.addWidget(self.header)
        header_row.addStretch(1)
        header_row.addWidget(more)
        layout.addLayout(header_row)
        if video.over_quote:
            note = QLabel(
                f"Charged ${video.charged or video.cost:.2f}, more than the "
                f"${video.quote:.2f} shown before it was sent."
            )
            note.setObjectName("warningLabel")
            note.setWordWrap(True)
            layout.addWidget(note)

        poster = pixmap_within(item.poster, QSize(920, VIDEO_MAX_HEIGHT))
        aspect = poster.size() if not poster.isNull() else _aspect_of(video)
        self.surface = VideoSurface(poster, aspect)
        self.surface.setToolTip("Click to play or pause")
        self.surface.clicked.connect(self.toggle)
        layout.addWidget(self.surface)

        self.play_button = QToolButton()
        self.play_button.setObjectName("messageAction")
        self.play_button.setText("▶")
        self.play_button.clicked.connect(self.toggle)
        self.position = QSlider(Qt.Horizontal)
        self.position.setRange(0, 0)
        self.position.sliderMoved.connect(self._seek)
        self.time = QLabel("")
        self.time.setObjectName("hintLabel")
        self.mute = QToolButton()
        self.mute.setObjectName("messageAction")
        self.mute.setText("🔊")
        self.mute.setToolTip("Sound on or off")
        self.mute.clicked.connect(self._toggle_mute)
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(self.play_button)
        controls.addWidget(self.position, 1)
        controls.addWidget(self.time)
        controls.addWidget(self.mute)
        self.controls = QWidget()
        self.controls.setLayout(controls)
        self.controls.hide()
        layout.addWidget(self.controls)
        self.error = QLabel("")
        self.error.setObjectName("warningLabel")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        self.surface.on_position = self._show_position
        self.surface.on_playing = self._show_playing
        self.surface.on_error = self._show_error
        self.surface.on_stopped = lambda: self._show_playing(False)

    def _act(self, key: str) -> None:
        if key == "play":
            self.toggle()
        else:
            self.action_requested.emit(key, self.video_id)

    def toggle(self) -> None:
        if not multimedia_available():
            self._show_error("Playing needs Qt's multimedia module, which isn't installed.")
            return
        player = playback()
        if player.surface is self.surface:
            player.toggle()
            return
        source = self._source()
        if source is None:
            self._show_error("The video's file is missing.")
            return
        self.controls.show()
        player.play(self.surface, source)

    def _seek(self, position: int) -> None:
        player = playback()
        if player.surface is self.surface:
            player.player.setPosition(position)

    def _toggle_mute(self) -> None:
        self.surface.muted = not self.surface.muted
        self.mute.setText("🔇" if self.surface.muted else "🔊")
        player = playback()
        if player.surface is self.surface:
            player.audio.setMuted(self.surface.muted)

    def _show_position(self, position: int, duration: int) -> None:
        if duration > 0 and self.position.maximum() != duration:
            self.position.setRange(0, duration)
        if not self.position.isSliderDown():
            self.position.setValue(position)
        self.time.setText(f"{_clock(position)} / {_clock(duration)}")

    def _show_playing(self, playing: bool) -> None:
        self.play_button.setText("❚❚" if playing else "▶")

    def _show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.show()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Dropped from the transcript's built window, or the transcript gave
        # way to the story map: let go of the player. (Not for the window
        # being minimised: that hide is the system's, "spontaneous".)
        if _playback is not None and not event.spontaneous():
            _playback.release(self.surface)
        super().hideEvent(event)


def _aspect_of(video: GeneratedVideo) -> QSize:
    ratio = str(video.settings.get("aspect_ratio") or "16:9")
    try:
        w, h = (int(float(x) * 100) for x in ratio.split(":"))
    except ValueError:
        w, h = 16, 9
    return QSize(max(w, 1), max(h, 1))


class PendingVideoWidget(QFrame):
    """A video being made, or one that failed, where it will appear."""

    # (action, video id): stop, check, delete.
    action_requested = Signal(str, str)

    def __init__(self, item: VideoItem) -> None:
        super().__init__()
        video = item.record
        self.setObjectName("message")
        self.setProperty("kind", "picture")
        self.video_id = video.id
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        model = video.model.rsplit("/", 1)[-1]
        paid = video.charged if video.charged is not None else video.quote
        cost = f" Charged ${paid:.2f} when it was sent." if paid is not None else ""
        if video.status == "failed":
            text = f"The video from {model} failed: {video.error or 'no reason given'}."
            button, tip, key = "Delete", "Remove this note.", "delete"
        elif item.running:
            where = f" ({video.progress.lower().replace('_', ' ')})" if video.progress else ""
            text = (
                f"Making a video with {model}{where}… It takes a few minutes; play on, "
                f"it appears here when it's done.{cost}"
            )
            button = "Stop waiting"
            tip = (
                "Stop asking after it for now. It is still being made and paid for; "
                "Check again, or open the story later, and it is fetched."
            )
            key = "stop"
        else:
            text = f"A video from {model} is still being made or waiting to be fetched.{cost}"
            button, tip, key = "Check again", "Ask the service after it now.", "check"
        label = QLabel(text)
        label.setObjectName("messageHeader")
        label.setWordWrap(True)
        label.setToolTip(video.prompt)
        action = QToolButton()
        action.setObjectName("messageAction")
        action.setText(button)
        action.setToolTip(tip)
        action.clicked.connect(lambda: self.action_requested.emit(key, self.video_id))
        layout.addWidget(label, 1)
        layout.addWidget(action)
