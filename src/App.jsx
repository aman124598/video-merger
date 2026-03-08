import { useMemo, useState } from "react";

const MAX_FILES = 10;

function toFixedTime(value) {
  if (!Number.isFinite(value)) return "0.00";
  return value.toFixed(2);
}

function App() {
  const [videos, setVideos] = useState([]);
  const [outputName, setOutputName] = useState("merged_output.mp4");
  const [isMerging, setIsMerging] = useState(false);
  const [status, setStatus] = useState("Add videos, pick ranges, then merge.");
  const [error, setError] = useState("");
  const [downloadUrl, setDownloadUrl] = useState("");

  const canMerge = videos.length > 0 && videos.every((v) => v.duration > 0 && v.end > v.start) && !isMerging;

  const totalDuration = useMemo(
    () => videos.reduce((acc, v) => acc + Math.max(0, v.end - v.start), 0),
    [videos]
  );

  const onSelectFiles = (event) => {
    setError("");
    setDownloadUrl("");
    const chosen = Array.from(event.target.files || []);
    if (!chosen.length) return;

    const remaining = MAX_FILES - videos.length;
    if (remaining <= 0) {
      setError("Maximum 10 videos allowed.");
      return;
    }

    const selected = chosen.slice(0, remaining).map((file) => ({
      id: crypto.randomUUID(),
      file,
      previewUrl: URL.createObjectURL(file),
      duration: 0,
      start: 0,
      end: 0
    }));

    setVideos((prev) => [...prev, ...selected]);

    if (chosen.length > remaining) {
      setError(`Only ${remaining} file(s) were added. Maximum is 10 videos.`);
    }
  };

  const onLoadedMetadata = (id, duration) => {
    setVideos((prev) =>
      prev.map((video) =>
        video.id === id
          ? {
              ...video,
              duration,
              start: 0,
              end: duration
            }
          : video
      )
    );
  };

  const updateRange = (id, key, value) => {
    const numeric = Number(value);
    setVideos((prev) =>
      prev.map((video) => {
        if (video.id !== id) return video;
        const next = { ...video, [key]: numeric };
        if (key === "start" && next.start >= next.end) {
          next.end = Math.min(video.duration, next.start + 0.1);
        }
        if (key === "end" && next.end <= next.start) {
          next.start = Math.max(0, next.end - 0.1);
        }
        return next;
      })
    );
  };

  const moveVideo = (index, offset) => {
    const target = index + offset;
    if (target < 0 || target >= videos.length) return;
    setVideos((prev) => {
      const next = [...prev];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  const removeVideo = (id) => {
    setVideos((prev) => {
      const found = prev.find((v) => v.id === id);
      if (found?.previewUrl) URL.revokeObjectURL(found.previewUrl);
      return prev.filter((v) => v.id !== id);
    });
  };

  const mergeVideos = async () => {
    setIsMerging(true);
    setError("");
    setDownloadUrl("");
    setStatus("Uploading videos...");

    try {
      const formData = new FormData();
      videos.forEach((video, index) => {
        formData.append("videos", video.file);
        formData.append(
          "clips",
          JSON.stringify({
            index,
            start: Number(video.start),
            end: Number(video.end),
            name: video.file.name
          })
        );
      });
      formData.append("outputName", outputName.trim() || "merged_output.mp4");

      setStatus("Merging with FFmpeg...");
      const response = await fetch("/api/merge", {
        method: "POST",
        body: formData
      });
      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.error || "Merge failed");
      }

      setDownloadUrl(data.downloadUrl);
      setStatus("Merge complete. Download the output.");
    } catch (err) {
      setError(err.message || "Failed to merge videos");
      setStatus("Merge failed.");
    } finally {
      setIsMerging(false);
    }
  };

  return (
    <div className="app">
      <header className="header">
        <h1>Video Merge Dashboard</h1>
        <p>Preview each clip, set start/end points, then merge all clips in order.</p>
      </header>

      <section className="panel controls">
        <label className="input-group">
          <span>Add Videos (up to 10)</span>
          <input type="file" accept="video/*" multiple onChange={onSelectFiles} disabled={isMerging} />
        </label>

        <label className="input-group">
          <span>Output file name</span>
          <input
            type="text"
            value={outputName}
            onChange={(e) => setOutputName(e.target.value)}
            disabled={isMerging}
            placeholder="merged_output.mp4"
          />
        </label>

        <button className="primary-btn" disabled={!canMerge} onClick={mergeVideos}>
          {isMerging ? "Merging..." : "Merge Selected Clips"}
        </button>
      </section>

      <section className="panel summary">
        <p>Total clips: {videos.length}</p>
        <p>Total selected duration: {toFixedTime(totalDuration)} sec</p>
        <p>Status: {status}</p>
        {error ? <p className="error">{error}</p> : null}
        {downloadUrl ? (
          <a className="download-link" href={downloadUrl}>
            Download merged video
          </a>
        ) : null}
      </section>

      <section className="grid">
        {videos.map((video, index) => (
          <article key={video.id} className="panel card">
            <div className="card-header">
              <strong>{index + 1}. {video.file.name}</strong>
              <div className="card-actions">
                <button disabled={isMerging || index === 0} onClick={() => moveVideo(index, -1)}>Up</button>
                <button disabled={isMerging || index === videos.length - 1} onClick={() => moveVideo(index, 1)}>Down</button>
                <button disabled={isMerging} onClick={() => removeVideo(video.id)}>Remove</button>
              </div>
            </div>

            <video
              controls
              src={video.previewUrl}
              onLoadedMetadata={(e) => onLoadedMetadata(video.id, e.currentTarget.duration)}
            />

            <div className="range-box">
              <label>
                Start: {toFixedTime(video.start)}s
                <input
                  type="range"
                  min="0"
                  max={video.duration || 0}
                  step="0.1"
                  value={video.start}
                  disabled={isMerging || !video.duration}
                  onChange={(e) => updateRange(video.id, "start", e.target.value)}
                />
              </label>

              <label>
                End: {toFixedTime(video.end)}s
                <input
                  type="range"
                  min="0"
                  max={video.duration || 0}
                  step="0.1"
                  value={video.end}
                  disabled={isMerging || !video.duration}
                  onChange={(e) => updateRange(video.id, "end", e.target.value)}
                />
              </label>
            </div>

            <p className="clip-duration">
              Clip duration: {toFixedTime(Math.max(0, video.end - video.start))}s / Source: {toFixedTime(video.duration)}s
            </p>
          </article>
        ))}
      </section>
    </div>
  );
}

export default App;
