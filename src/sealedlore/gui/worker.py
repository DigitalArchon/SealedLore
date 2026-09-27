"""Generation on a worker thread.

All API work happens here and reaches the GUI only as signals; no widget is
ever touched off the GUI thread. The worker also owns the session while a
generation is in flight — the window disables the controls that would mutate
story state until `finished` arrives, so there is one writer at a time.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from PySide6.QtCore import QObject, Signal, Slot

from sealedlore.engine.session import PassageDone, SessionNotice
from sealedlore.providers.base import (
    ChatProvider,
    ReasoningDelta,
    StreamCancelled,
    StreamEvent,
    TextDelta,
    error_detail,
)

WorkerEvent = StreamEvent | SessionNotice | PassageDone


class GenerationWorker(QObject):
    text_delta = Signal(str)
    reasoning_delta = Signal(str)
    # Archival and anything else the session does on the way to the response.
    notice = Signal(str)
    # The passage is in; the rest of the job is background reads.
    passage_done = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        start: Callable[[], Iterator[WorkerEvent]],
        *,
        provider: ChatProvider | None = None,
    ) -> None:
        super().__init__()
        self._start = start
        # Stop cancels the provider's request at once (its cancel()) rather
        # than at the next chunk — which, from a stalled connection, a slow
        # first token or a job that doesn't stream, could be minutes away.
        self._provider = provider
        self._events: Iterator[WorkerEvent] | None = None
        self._stopping = False

    @Slot()
    def run(self) -> None:
        try:
            if self._provider is not None:
                # A cancel left over from an earlier job mustn't stop this one;
                # one for this job may already have come, so check after.
                self._provider.reset_cancel()
                if self._stopping:
                    return
            self._events = self._start()
            for event in self._events:
                if isinstance(event, TextDelta):
                    self.text_delta.emit(event.text)
                elif isinstance(event, ReasoningDelta):
                    self.reasoning_delta.emit(event.text)
                elif isinstance(event, SessionNotice):
                    self.notice.emit(event.text)
                elif isinstance(event, PassageDone):
                    self.passage_done.emit()
                if self._stopping:
                    break
        except StreamCancelled:
            pass  # the author pressed Stop; the partial text is already kept
        except Exception as exc:  # surfaced in the UI rather than killing the thread
            self.failed.emit(error_detail(exc))
        finally:
            self._close()
            self.finished.emit()

    @Slot()
    def stop(self) -> None:
        """End the stream now. The partial text is kept (§8.1).

        Called on the GUI thread; the cancel only shuts the socket down, and
        the worker's own thread sees the stream end and cleans up.
        """
        self._stopping = True
        if self._provider is not None:
            self._provider.cancel()

    def _close(self) -> None:
        # Closing the generator runs the session's finalizer, which writes the
        # partial text to a node and saves. Without this the node would only
        # appear whenever the generator happened to be collected.
        if self._events is not None:
            events, self._events = self._events, None
            close = getattr(events, "close", None)
            if close is not None:
                close()
