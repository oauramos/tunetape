"""A stand-in for yt-dlp that needs no network (``TUNETAPE_YTDLP``).

Covers the two invocations tunetape makes:

- ``--get-url --get-title <input>`` prints a title line and an http "stream
  URL" (which the fake mpv happily "plays"). The title is ``Fake: <input>``;
  an input containing ``fake-unavailable`` fails like a private video.
- ``--flat-playlist --print …`` lists three videos ``vid1``–``vid3``.
"""

import sys


def main(argv):
    target = argv[-1] if argv else ""
    if "fake-unavailable" in target:
        sys.stderr.write("ERROR: [youtube] fake: Video unavailable\n")
        return 1
    if "--flat-playlist" in argv:
        for n in (1, 2, 3):
            print("vid%d|Playlist video %d" % (n, n))
        return 0
    if "--get-url" in argv:
        query = target.split(":", 1)[1] if target.startswith("ytsearch") else target
        print("Fake: %s" % query)
        print("https://media.invalid/stream.webm?for=%s" % len(target))
        return 0
    sys.stderr.write("fake yt-dlp: unsupported arguments %r\n" % (argv,))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
