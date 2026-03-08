# Video Merger Dashboard (React + FFmpeg)

Visual dashboard to:
- Select up to 10 videos
- Preview each video
- Pick start/end range for each clip
- Merge selected ranges into one MP4 using FFmpeg normalization + concat

## Prerequisites

- Node.js 18+
- FFmpeg and FFprobe available in PATH

Check:

```powershell
ffmpeg -version
ffprobe -version
```

## Install

```powershell
npm install
```

## Run

Open 2 terminals in `y:\video-merger`:

1) Start backend server:

```powershell
npm run server
```

2) Start React dev server:

```powershell
npm run dev
```

Then open:

`http://localhost:5173`

## How merge works

1. Browser uploads selected videos and selected ranges.
2. Server probes each input with `ffprobe`.
3. Each range is trimmed and normalized (H.264/AAC, consistent resolution/fps/audio).
4. Server concatenates segments (`-c copy` first, re-encode fallback).
5. You get a download link for merged `.mp4`.
