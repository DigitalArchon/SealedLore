"""Phone access: a story played from a phone's Tor Browser.

SealedLore runs its own tor, publishes an onion service, and serves a small
page to the phones the author has paired, for the stories they chose to
share. This package is the part with no Qt in it: the QR code the phone
scans, the HTTP the page speaks, pairing, the event stream and the API's
rules. The window (`gui/window_phone.py`) answers it from the story.
"""
