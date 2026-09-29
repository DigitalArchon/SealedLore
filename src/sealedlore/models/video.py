"""Videos made in play (videos.json), from the moment the endpoint accepts one.

A video is a job that takes minutes and is paid for when it is sent, so its
record is written as soon as the endpoint accepts it (`status="pending"`,
with what is needed to ask after it again), and the app carries on asking
after it when the story is opened again, until it is done or has failed.
Like a picture, it hangs off the passage it shows and outlives it.

Files live under the story's `images/videos/`: the video, its first frame
(the poster shown before it plays) and its last frame (offered as the start
of the next video).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.image import RefUse

VideoStatus = Literal["pending", "done", "failed"]


class GeneratedVideo(BaseModel):
    id: str = Field(default_factory=new_id)
    anchor_node_id: str | None = None
    status: VideoStatus = "pending"
    # Relative to the story folder; empty until it is done.
    file: str = ""
    poster: str = ""
    last_frame: str = ""
    # Exactly the prompt the author approved and the model was sent.
    prompt: str
    direction: str = ""
    model: str
    # The settings sent, by the model's own names ("resolution", "duration").
    settings: dict[str, Any] = Field(default_factory=dict)
    start_frame: RefUse | None = None
    end_frame: RefUse | None = None
    # Where it is being made: the service, its address and the job there.
    api: str
    base_url: str
    job_id: str = ""
    poll_url: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    finished_at: str | None = None
    # The price worked out before it was sent (None: none could be), what
    # the endpoint said it charged on accepting it, and the final cost.
    quote: float | None = None
    charged: float | None = None
    cost: float | None = None
    cost_reported: bool = False
    error: str = ""
    # What the endpoint last said about it ("IN_QUEUE", "processing").
    progress: str = ""
    duration: float | None = None
    request_log_ref: str | None = None
    # Made in a private scene: its direction came from the scene.
    private_span: str | None = None

    @property
    def seconds(self) -> str:
        value = self.settings.get("duration")
        return f"{value} s" if value not in (None, "") else ""

    @property
    def over_quote(self) -> bool:
        """Charged noticeably more than the price shown before it was sent."""
        paid = self.charged if self.charged is not None else self.cost
        return self.quote is not None and paid is not None and paid > self.quote * 1.05 + 0.005
