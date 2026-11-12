"""Generate local QR SVGs without contacting an external service."""

from __future__ import annotations

import io

import segno


def qr_svg(uri: str) -> str:
    output = io.BytesIO()
    segno.make(uri, micro=False).save(output, kind="svg", scale=5, xmldecl=False)
    return output.getvalue().decode("utf-8")
