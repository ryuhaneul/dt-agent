# Media File Tools

Scripts for processing files received via any transport (Telegram, Matrix, API, web chat).

## Incoming file prompts

`[INCOMING FILE]` blocks identify the source (including Web for web chat), stored
`Path`, MIME `Type` and `Original filename`. Use the stored path to inspect the
file; the original filename is a display label. Resolve workspace-relative paths
against the workspace before passing them to tools that require absolute paths.
A web message can include several file blocks with an optional user caption.

Web uploads live under `webchat_files/YYYY-MM-DD/` with random storage names;
Telegram uploads remain under `telegram_files/`. Both use the same file-type
routing below. Web images are preserved unless the user explicitly chose browser
resizing/compression; do not assume that every incoming image was normalized to
WebP. Thumbnail files are display previews, not the uploaded original.

## Common Commands

```bash
python3 tools/media_tools/list_files.py --limit 20
python3 tools/media_tools/list_files.py --type image
python3 tools/media_tools/list_files.py --date 2026-01-15
python3 tools/media_tools/file_info.py --file /absolute/path/to/file
python3 tools/media_tools/read_document.py --file /absolute/path/to/doc.pdf
python3 tools/media_tools/transcribe_audio.py --file /absolute/path/to/audio.ogg
python3 tools/media_tools/process_video.py --file /absolute/path/to/video.mp4
```

## File-Type Routing

- image/photo: inspect directly
- audio/voice: transcribe first
- document/PDF: extract text
- video: frames + transcript
- sticker: acknowledge naturally

## Dependencies

- always available: `file_info.py`, `list_files.py`
- PDF parsing: `pypdf`
- YAML listing: `pyyaml`
- audio transcription: OpenAI API key or local Whisper variants
- video processing: `ffmpeg`

## Response UX

After processing, offer concise next actions (optional buttons) when helpful.
