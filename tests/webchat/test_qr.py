"""QR rendering for TOTP enrollment."""

from __future__ import annotations

import re

from ductor_bot.webchat.web.qr import qr_svg


def test_qr_svg_scales_to_its_container() -> None:
    svg = qr_svg("otpauth://totp/DT%20Agent:main?secret=JBSWY3DPEHPK3PXP&issuer=DT%20Agent")
    root = re.match(r"<svg[^>]*>", svg)
    assert root is not None
    # A fixed width/height without viewBox makes CSS sizing crop the code.
    assert "viewBox=" in root.group(0)
    assert " width=" not in root.group(0)
    assert " height=" not in root.group(0)
