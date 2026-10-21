# files/

Shared transport-agnostic file helpers used by Telegram, Matrix, and direct API paths.

## Files

- `files/allowed_roots.py`: `resolve_allowed_roots(file_access, workspace)`
- `files/tags.py`: file-tag parsing, MIME detection, media classification
- `files/storage.py`: filename sanitization + destination generation
- `files/prompt.py`: incoming-file prompt builder (`MediaInfo`, `build_media_prompt`)
- `files/image_processor.py`: incoming image resize and format conversion

## Purpose

Share file helpers across Telegram, Matrix and API for:

- `<file:...>` parsing
- MIME/type detection
- safe upload/download path handling
- incoming media prompt construction

## Core helpers

### `resolve_allowed_roots(...)`

Maps `file_access` to allowed roots:

- `all` -> unrestricted (`None`)
- `home` -> `[Path.home()]`
- `workspace` -> `[workspace]`
- unknown -> `[workspace]` fallback (restrictive)

### `sanitize_filename(name)`

- strips separators/unsafe chars
- normalizes repeated separators
- truncates long names
- fallback `"file"`

### `prepare_destination(base_dir, file_name)`

- uses date folder `YYYY-MM-DD`
- creates directories as needed
- de-duplicates via `_1`, `_2`, ... suffix

### `tags` helpers

- parse `<file:...>` tags and file URIs
- normalize Windows path variants
- detect MIME via `filetype` with extension fallback
- classify media as `photo|audio|video|document`

### `build_media_prompt(info, workspace, transport=...)`

Builds standardized `[INCOMING FILE]` prompt blocks for agent input.

Current prompt behavior:

- points agents at `tools/media_tools/CLAUDE.md` for file-handling instructions
- audio/voice files point to `tools/media_tools/transcribe_audio.py --file ...`
- video files point to `tools/media_tools/process_video.py --file ...`
- paths are rewritten relative to the workspace when possible so the same prompt works on host and in Docker

### Image processing (`image_processor.py`)

`process_image(path, *, max_dimension, output_format, quality)` resizes and converts incoming images:

- images exceeding `max_dimension` (default 2000px) are proportionally resized using Lanczos resampling
- output is converted to the target format (default WebP) with configurable quality (default 85)
- GIF and APNG MIME types are skipped and returned as-is, whether or not animated
- images already at or below the size limit and in the target format are returned unchanged
- on any processing error, the original file is used as fallback (non-fatal)

`ImageConfig` exposes `max_dimension`, `output_format`, and `quality`, but the
current Telegram, Matrix and API call sites invoke `process_image(dest)` without
those config values. They use the helper defaults above; changing `config.image`
does not currently change incoming-image processing.

External transcription hand-off:

- `config.transcription.audio_command` is exported to CLI subprocesses as `DUCTOR_TRANSCRIBE_COMMAND`
- `config.transcription.video_command` is exported as `DUCTOR_VIDEO_TRANSCRIBE_COMMAND`
- the bundled `tools/media_tools/transcribe_audio.py` and `process_video.py` scripts consume those env vars when present and otherwise fall back to built-in behavior

Applied across all transports:

- Telegram: `messenger/telegram/media.py`
- Matrix: `messenger/matrix/media.py`
- API: `api/server.py` (upload endpoint)

## Integration points

- Telegram media ingest/send: `messenger/telegram/media.py`, `messenger/telegram/sender.py`, `messenger/telegram/app.py`
- Matrix media ingest: `messenger/matrix/media.py`, `messenger/matrix/bot.py`
- API upload/download and file-ref extraction: `api/server.py`
- API startup file-context wiring: `orchestrator/lifecycle.py`

## Runtime paths

- Telegram uploads: `~/.ductor/workspace/telegram_files/YYYY-MM-DD/`
- Matrix uploads: `~/.ductor/workspace/matrix_files/YYYY-MM-DD/`
- API uploads: `~/.ductor/workspace/api_files/YYYY-MM-DD/`
