"""Source-agnostic playback data model.

A ``Track`` is one playable item and an ``Album`` is an ordered collection of
them. These are deliberately neutral so every source adapter (KHInsider,
Spotify, YouTube playlists) can build the same shapes and feed the shared
``Playlist`` / ``MPVController`` / ``PlayerUI`` machinery.

``resolve_hint`` is whatever a source's resolver needs to turn the track into a
streamable URL — a KHInsider track-page URL, a YouTube watch URL, or a
``ytsearch1:…`` query for Spotify. The resolved stream URL is cached on
``direct_url``.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Track:
    name: str
    resolve_hint: str
    direct_url: Optional[str] = None


@dataclass
class Album:
    title: str
    tracks: list = field(default_factory=list)
