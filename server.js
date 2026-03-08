import express from "express";
import multer from "multer";
import fs from "fs";
import fsp from "fs/promises";
import os from "os";
import path from "path";
import crypto from "crypto";
import { spawn } from "child_process";

const app = express();
const PORT = process.env.PORT || 4000;
const ROOT = process.cwd();
const UPLOAD_DIR = path.join(ROOT, "tmp_uploads");
const OUTPUT_DIR = path.join(ROOT, "outputs");
const MAX_FILES = 10;

await ensureDir(UPLOAD_DIR);
await ensureDir(OUTPUT_DIR);

const upload = multer({
  dest: UPLOAD_DIR,
  limits: {
    files: MAX_FILES,
    fileSize: 1024 * 1024 * 1024
  }
});

function ensureDir(dir) {
  return fsp.mkdir(dir, { recursive: true });
}

function parseRatio(value, fallback = 30) {
  if (!value || value === "0/0") return fallback;
  const [a, b] = String(value).split("/").map(Number);
  if (!Number.isFinite(a) || !Number.isFinite(b) || b === 0) return fallback;
  return a / b;
}

function even(value) {
  const v = Math.max(2, Math.floor(value));
  return v % 2 === 0 ? v : v - 1;
}

function runProcess(bin, args) {
  return new Promise((resolve, reject) => {
    const proc = spawn(bin, args, { windowsHide: true });
    let stderr = "";
    let stdout = "";
    proc.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    proc.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    proc.on("error", reject);
    proc.on("close", (code) => {
      if (code === 0) resolve({ stdout, stderr });
      else reject(new Error(stderr || stdout || `${bin} failed with code ${code}`));
    });
  });
}

async function checkFfmpeg() {
  await runProcess("ffmpeg", ["-version"]);
  await runProcess("ffprobe", ["-version"]);
}

async function probeVideo(inputPath) {
  const { stdout } = await runProcess("ffprobe", [
    "-v",
    "error",
    "-print_format",
    "json",
    "-show_format",
    "-show_streams",
    inputPath
  ]);
  const data = JSON.parse(stdout);
  const streams = data.streams || [];
  const format = data.format || {};
  const video = streams.find((s) => s.codec_type === "video");
  const audio = streams.find((s) => s.codec_type === "audio");
  if (!video) throw new Error(`No video stream found for ${path.basename(inputPath)}`);

  const duration = Number(video.duration ?? format.duration ?? 0);
  if (!Number.isFinite(duration) || duration <= 0) {
    throw new Error(`Invalid duration for ${path.basename(inputPath)}`);
  }
  const width = Number(video.width || 0);
  const height = Number(video.height || 0);
  if (!width || !height) {
    throw new Error(`Invalid dimensions for ${path.basename(inputPath)}`);
  }

  return {
    duration,
    width,
    height,
    fps: parseRatio(video.avg_frame_rate || video.r_frame_rate, 30),
    sampleRate: audio?.sample_rate ? Number(audio.sample_rate) : 48000,
    hasAudio: Boolean(audio)
  };
}

function buildSegmentArgs({ inputPath, outputPath, start, duration, meta, target }) {
  const width = even(target.width);
  const height = even(target.height);
  const fps = Math.max(1, target.fps || 30);
  const sampleRate = Number.isFinite(target.sampleRate) && target.sampleRate > 8000 ? target.sampleRate : 48000;

  const baseFilters =
    `[0:v]scale=${width}:${height}:force_original_aspect_ratio=decrease,` +
    `pad=${width}:${height}:(ow-iw)/2:(oh-ih)/2:black,` +
    `fps=${fps},format=yuv420p,setsar=1[v]`;

  if (meta.hasAudio) {
    const filter =
      `${baseFilters};` +
      `[0:a]aformat=sample_fmts=fltp:sample_rates=${sampleRate}:channel_layouts=stereo[a0];` +
      `[a0]aresample=${sampleRate}[a]`;

    return [
      "-y",
      "-hide_banner",
      "-loglevel",
      "error",
      "-ss",
      String(start),
      "-t",
      String(duration),
      "-i",
      inputPath,
      "-filter_complex",
      filter,
      "-map",
      "[v]",
      "-map",
      "[a]",
      "-c:v",
      "libx264",
      "-preset",
      "medium",
      "-crf",
      "20",
      "-pix_fmt",
      "yuv420p",
      "-c:a",
      "aac",
      "-b:a",
      "192k",
      "-ar",
      String(sampleRate),
      "-ac",
      "2",
      "-movflags",
      "+faststart",
      outputPath
    ];
  }

  const filter = `${baseFilters};[1:a]anull[a]`;
  return [
    "-y",
    "-hide_banner",
    "-loglevel",
    "error",
    "-ss",
    String(start),
    "-t",
    String(duration),
    "-i",
    inputPath,
    "-f",
    "lavfi",
    "-t",
    String(duration),
    "-i",
    `anullsrc=channel_layout=stereo:sample_rate=${sampleRate}`,
    "-filter_complex",
    filter,
    "-map",
    "[v]",
    "-map",
    "[a]",
    "-shortest",
    "-c:v",
    "libx264",
    "-preset",
    "medium",
    "-crf",
    "20",
    "-pix_fmt",
    "yuv420p",
    "-c:a",
    "aac",
    "-b:a",
    "192k",
    "-ar",
    String(sampleRate),
    "-ac",
    "2",
    "-movflags",
    "+faststart",
    outputPath
  ];
}

function sanitizeOutputName(name) {
  const cleaned = String(name || "merged_output.mp4").replace(/[<>:"/\\|?*\x00-\x1F]/g, "_").trim();
  return cleaned.toLowerCase().endsWith(".mp4") ? cleaned : `${cleaned || "merged_output"}.mp4`;
}

async function safeUnlink(filePath) {
  try {
    await fsp.unlink(filePath);
  } catch {
    // no-op
  }
}

app.get("/api/health", (_, res) => {
  res.json({ ok: true });
});

app.post("/api/merge", upload.array("videos", MAX_FILES), async (req, res) => {
  const uploadedFiles = req.files || [];
  const tempRunDir = await fsp.mkdtemp(path.join(os.tmpdir(), "video-merge-"));
  const generated = [];
  const toCleanup = uploadedFiles.map((f) => f.path);

  try {
    if (!uploadedFiles.length) {
      return res.status(400).json({ error: "Please upload at least one video." });
    }
    if (uploadedFiles.length > MAX_FILES) {
      return res.status(400).json({ error: "Maximum 10 videos are allowed." });
    }

    const clipsRaw = req.body.clips;
    const clips = (Array.isArray(clipsRaw) ? clipsRaw : [clipsRaw])
      .filter(Boolean)
      .map((entry) => JSON.parse(entry));

    if (clips.length !== uploadedFiles.length) {
      return res.status(400).json({ error: "Clip metadata does not match uploaded videos." });
    }

    await checkFfmpeg();

    const probed = [];
    for (let i = 0; i < uploadedFiles.length; i += 1) {
      const file = uploadedFiles[i];
      const clip = clips.find((c) => Number(c.index) === i);
      if (!clip) {
        throw new Error(`Missing clip metadata for file index ${i + 1}`);
      }
      const meta = await probeVideo(file.path);
      const start = Number(clip.start);
      const end = Number(clip.end);
      if (!Number.isFinite(start) || !Number.isFinite(end)) {
        throw new Error(`Invalid start/end for file: ${clip.name || file.originalname}`);
      }
      if (start < 0 || end <= start) {
        throw new Error(`Range must satisfy end > start for file: ${clip.name || file.originalname}`);
      }
      if (end > meta.duration + 0.01) {
        throw new Error(
          `Selected end time ${end.toFixed(2)}s exceeds source duration ${meta.duration.toFixed(2)}s for ${clip.name || file.originalname}`
        );
      }
      probed.push({
        file,
        clip,
        meta,
        start,
        duration: end - start
      });
    }

    const first = probed[0].meta;
    const target = {
      width: first.width,
      height: first.height,
      fps: first.fps,
      sampleRate: first.sampleRate
    };

    const segmentPaths = [];
    for (let i = 0; i < probed.length; i += 1) {
      const item = probed[i];
      const segmentPath = path.join(tempRunDir, `segment_${String(i + 1).padStart(2, "0")}.mp4`);
      const args = buildSegmentArgs({
        inputPath: item.file.path,
        outputPath: segmentPath,
        start: item.start,
        duration: item.duration,
        meta: item.meta,
        target
      });
      await runProcess("ffmpeg", args);
      segmentPaths.push(segmentPath);
      generated.push(segmentPath);
    }

    const concatPath = path.join(tempRunDir, "concat.txt");
    const concatBody = segmentPaths.map((p) => `file '${p.replace(/'/g, "'\\''")}'`).join("\n");
    await fsp.writeFile(concatPath, concatBody, "utf8");
    generated.push(concatPath);

    const outputFileName = `${Date.now()}_${crypto.randomBytes(4).toString("hex")}_${sanitizeOutputName(req.body.outputName)}`;
    const outputPath = path.join(OUTPUT_DIR, outputFileName);

    try {
      await runProcess("ffmpeg", [
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concatPath,
        "-c",
        "copy",
        outputPath
      ]);
    } catch {
      await runProcess("ffmpeg", [
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concatPath,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        outputPath
      ]);
    }

    return res.json({
      ok: true,
      downloadUrl: `/api/download/${encodeURIComponent(outputFileName)}`
    });
  } catch (err) {
    return res.status(500).json({ error: err.message || "Merge failed." });
  } finally {
    for (const p of toCleanup) {
      await safeUnlink(p);
    }
    for (const p of generated) {
      await safeUnlink(p);
    }
    await fsp.rm(tempRunDir, { recursive: true, force: true });
  }
});

app.get("/api/download/:name", async (req, res) => {
  const raw = req.params.name || "";
  const safeName = path.basename(raw);
  const abs = path.join(OUTPUT_DIR, safeName);
  if (!fs.existsSync(abs)) {
    return res.status(404).json({ error: "File not found." });
  }
  return res.download(abs, safeName);
});

app.listen(PORT, () => {
  console.log(`Server listening on http://localhost:${PORT}`);
});
