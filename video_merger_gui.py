import json
import math
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


class MergeCancelled(Exception):
    pass


@dataclass
class VideoMetadata:
    duration: float
    width: int
    height: int
    fps: float
    sample_rate: int
    has_audio: bool


@dataclass
class ClipSpec:
    path: str
    duration: float
    metadata: VideoMetadata


@dataclass
class NormalizeTarget:
    width: int
    height: int
    fps: float
    sample_rate: int


def _parse_fps(value: str) -> float:
    if not value or value == "0/0":
        return 30.0
    try:
        return float(Fraction(value))
    except Exception:
        return 30.0


def _round_even(value: int) -> int:
    return max(2, int(math.floor(value / 2) * 2))


def _sanitize_sample_rate(value: Optional[int]) -> int:
    if not value:
        return 48000
    if value < 8000:
        return 48000
    return int(value)


def check_ffmpeg_binaries() -> None:
    for binary in ("ffmpeg", "ffprobe"):
        try:
            result = subprocess.run(
                [binary, "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or f"{binary} not usable")
        except FileNotFoundError as exc:
            raise RuntimeError(f"{binary} not found in PATH") from exc


def probe_video(path: str) -> VideoMetadata:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for '{path}': {proc.stderr.strip()}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid ffprobe output for '{path}'") from exc

    streams = data.get("streams", [])
    format_obj = data.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video_stream:
        raise RuntimeError(f"No video stream found in '{path}'")

    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration_value = (
        video_stream.get("duration")
        or format_obj.get("duration")
        or (audio_stream or {}).get("duration")
    )
    try:
        duration = float(duration_value)
    except (TypeError, ValueError):
        raise RuntimeError(f"Could not determine duration for '{path}'")
    if duration <= 0:
        raise RuntimeError(f"Invalid duration for '{path}'")

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Invalid dimensions for '{path}'")

    fps = _parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))
    sample_rate = _sanitize_sample_rate(
        int(audio_stream.get("sample_rate")) if audio_stream and audio_stream.get("sample_rate") else None
    )

    return VideoMetadata(
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        sample_rate=sample_rate,
        has_audio=audio_stream is not None,
    )


def _run_command(
    cmd: list[str],
    cancel_token: Optional[threading.Event] = None,
    process_ref: Optional[dict] = None,
) -> None:
    if cancel_token and cancel_token.is_set():
        raise MergeCancelled("Merge cancelled by user")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if process_ref is not None:
        process_ref["proc"] = proc

    while proc.poll() is None:
        if cancel_token and cancel_token.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise MergeCancelled("Merge cancelled by user")
        time.sleep(0.1)

    out, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.strip() or out.strip() or "ffmpeg command failed")


def build_segment_command(clip: ClipSpec, target: NormalizeTarget, out_path: str) -> list[str]:
    width = _round_even(target.width)
    height = _round_even(target.height)
    fps = max(1.0, float(target.fps))
    sr = _sanitize_sample_rate(target.sample_rate)

    video_filter = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={fps:.6f},format=yuv420p,setsar=1[v]"
    )

    if clip.metadata.has_audio:
        filter_complex = (
            f"{video_filter};"
            f"[0:a]aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts=stereo[a0];"
            f"[a0]aresample={sr}[a]"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0",
            "-t",
            str(clip.duration),
            "-i",
            clip.path,
            "-filter_complex",
            filter_complex,
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
            str(sr),
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            out_path,
        ]
    else:
        filter_complex = f"{video_filter};[1:a]anull[a]"
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0",
            "-t",
            str(clip.duration),
            "-i",
            clip.path,
            "-f",
            "lavfi",
            "-t",
            str(clip.duration),
            "-i",
            f"anullsrc=channel_layout=stereo:sample_rate={sr}",
            "-filter_complex",
            filter_complex,
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
            str(sr),
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            out_path,
        ]
    return cmd


def merge_segments(
    segment_paths: list[str],
    output_path: str,
    cancel_token: Optional[threading.Event] = None,
    process_ref: Optional[dict] = None,
) -> None:
    if not segment_paths:
        raise RuntimeError("No segments to merge")

    concat_file = Path(segment_paths[0]).parent / "concat.txt"
    with concat_file.open("w", encoding="utf-8") as handle:
        for segment in segment_paths:
            escaped = str(Path(segment).resolve()).replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")

    copy_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c",
        "copy",
        output_path,
    ]

    try:
        _run_command(copy_cmd, cancel_token=cancel_token, process_ref=process_ref)
        return
    except RuntimeError:
        pass

    reencode_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
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
        output_path,
    ]
    _run_command(reencode_cmd, cancel_token=cancel_token, process_ref=process_ref)


def run_merge_job(
    clips: list[ClipSpec],
    output_path: str,
    progress_cb: Callable[[str, float], None],
    cancel_token: threading.Event,
) -> None:
    if not clips:
        raise RuntimeError("No clips supplied")

    first = clips[0].metadata
    target = NormalizeTarget(
        width=first.width,
        height=first.height,
        fps=first.fps,
        sample_rate=first.sample_rate,
    )

    total_steps = len(clips) + 1
    process_ref: dict = {"proc": None}
    with tempfile.TemporaryDirectory(prefix="video_merger_") as tmpdir:
        tmp = Path(tmpdir)
        segment_paths: list[str] = []
        for idx, clip in enumerate(clips, start=1):
            progress_cb(f"Processing clip {idx}/{len(clips)}: {Path(clip.path).name}", (idx - 1) / total_steps)
            seg_path = str(tmp / f"segment_{idx:02d}.mp4")
            cmd = build_segment_command(clip, target, seg_path)
            _run_command(cmd, cancel_token=cancel_token, process_ref=process_ref)
            segment_paths.append(seg_path)

        progress_cb("Merging segments...", len(clips) / total_steps)
        merge_segments(
            segment_paths,
            output_path,
            cancel_token=cancel_token,
            process_ref=process_ref,
        )
        progress_cb("Done", 1.0)


class VideoMergerApp:
    MAX_FILES = 10

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Video Merger (FFmpeg)")
        self.root.geometry("900x560")

        self.files: list[dict] = []
        self.output_path = tk.StringVar()
        self.status_var = tk.StringVar(value="Select videos and durations, then click Merge.")
        self.progress_var = tk.DoubleVar(value=0.0)

        self.worker_thread: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
        self.ui_queue: Queue = Queue()

        self._build_ui()
        self._start_queue_poll()
        self._check_prereqs()

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        files_frame = ttk.LabelFrame(container, text="Input Videos (max 10)")
        files_frame.grid(row=0, column=0, sticky="nsew")
        files_frame.columnconfigure(0, weight=1)
        files_frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            files_frame,
            columns=("path", "duration"),
            show="headings",
            height=14,
            selectmode="browse",
        )
        self.tree.heading("path", text="Video File")
        self.tree.heading("duration", text="Duration (sec)")
        self.tree.column("path", width=650, anchor=tk.W)
        self.tree.column("duration", width=130, anchor=tk.CENTER)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", self._on_tree_double_click)

        scrollbar = ttk.Scrollbar(files_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        buttons_frame = ttk.Frame(files_frame)
        buttons_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        for i in range(5):
            buttons_frame.columnconfigure(i, weight=1)

        self.add_btn = ttk.Button(buttons_frame, text="Add Videos", command=self.add_files)
        self.remove_btn = ttk.Button(buttons_frame, text="Remove", command=self.remove_selected)
        self.up_btn = ttk.Button(buttons_frame, text="Move Up", command=lambda: self.move_selected(-1))
        self.down_btn = ttk.Button(buttons_frame, text="Move Down", command=lambda: self.move_selected(1))
        self.edit_btn = ttk.Button(buttons_frame, text="Set Duration", command=self.edit_selected_duration)
        self.add_btn.grid(row=0, column=0, padx=4, sticky="ew")
        self.remove_btn.grid(row=0, column=1, padx=4, sticky="ew")
        self.up_btn.grid(row=0, column=2, padx=4, sticky="ew")
        self.down_btn.grid(row=0, column=3, padx=4, sticky="ew")
        self.edit_btn.grid(row=0, column=4, padx=4, sticky="ew")

        output_frame = ttk.LabelFrame(container, text="Output")
        output_frame.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        output_frame.columnconfigure(0, weight=1)
        self.output_entry = ttk.Entry(output_frame, textvariable=self.output_path)
        self.output_entry.grid(row=0, column=0, sticky="ew", padx=(8, 6), pady=8)
        self.output_btn = ttk.Button(output_frame, text="Browse...", command=self.pick_output)
        self.output_btn.grid(row=0, column=1, padx=(0, 8), pady=8)

        actions_frame = ttk.Frame(container)
        actions_frame.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        actions_frame.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(actions_frame, variable=self.progress_var, maximum=1.0)
        self.progress.grid(row=0, column=0, columnspan=2, sticky="ew")

        self.status_label = ttk.Label(actions_frame, textvariable=self.status_var)
        self.status_label.grid(row=1, column=0, sticky="w", pady=(6, 0))

        button_row = ttk.Frame(actions_frame)
        button_row.grid(row=1, column=1, sticky="e", pady=(4, 0))
        self.merge_btn = ttk.Button(button_row, text="Merge", command=self.start_merge)
        self.cancel_btn = ttk.Button(button_row, text="Cancel", command=self.cancel_merge, state=tk.DISABLED)
        self.merge_btn.grid(row=0, column=0, padx=(0, 8))
        self.cancel_btn.grid(row=0, column=1)

    def _check_prereqs(self) -> None:
        try:
            check_ffmpeg_binaries()
        except Exception as exc:
            messagebox.showerror(
                "FFmpeg Missing",
                f"FFmpeg/FFprobe check failed:\n{exc}\n\nInstall FFmpeg and ensure both tools are in PATH.",
            )
            self._set_controls_enabled(False)
            self.status_var.set("FFmpeg not available.")

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for widget in (
            self.add_btn,
            self.remove_btn,
            self.up_btn,
            self.down_btn,
            self.edit_btn,
            self.output_entry,
            self.output_btn,
            self.merge_btn,
        ):
            widget.configure(state=state)

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self._set_controls_enabled(False)
            self.cancel_btn.configure(state=tk.NORMAL)
        else:
            self._set_controls_enabled(True)
            self.cancel_btn.configure(state=tk.DISABLED)

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for idx, item in enumerate(self.files):
            name = Path(item["path"]).name
            display = f"{idx + 1}. {name}"
            self.tree.insert(
                "",
                tk.END,
                iid=str(idx),
                values=(display, f"{item['duration']:.2f}" if item["duration"] is not None else ""),
            )

    def add_files(self) -> None:
        if len(self.files) >= self.MAX_FILES:
            messagebox.showwarning("Limit reached", "You can only add up to 10 videos.")
            return
        paths = filedialog.askopenfilenames(
            title="Select videos",
            filetypes=[("Video files", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("All files", "*.*")],
        )
        if not paths:
            return
        available = self.MAX_FILES - len(self.files)
        selected = list(paths)[:available]
        for path in selected:
            self.files.append({"path": path, "duration": None})
        self._refresh_tree()
        if len(paths) > available:
            messagebox.showinfo("Limit", f"Only first {available} files were added (max 10 total).")

    def remove_selected(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        idx = int(selected[0])
        self.files.pop(idx)
        self._refresh_tree()

    def move_selected(self, direction: int) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        idx = int(selected[0])
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(self.files):
            return
        self.files[idx], self.files[new_idx] = self.files[new_idx], self.files[idx]
        self._refresh_tree()
        self.tree.selection_set(str(new_idx))

    def _on_tree_double_click(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if item and column == "#2":
            self._edit_duration_for_index(int(item))

    def edit_selected_duration(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Select", "Select a row to set duration.")
            return
        self._edit_duration_for_index(int(selected[0]))

    def _edit_duration_for_index(self, idx: int) -> None:
        top = tk.Toplevel(self.root)
        top.title("Set Duration")
        top.resizable(False, False)
        top.transient(self.root)
        top.grab_set()

        ttk.Label(top, text=f"File: {Path(self.files[idx]['path']).name}").grid(
            row=0, column=0, columnspan=2, padx=10, pady=(10, 6), sticky="w"
        )
        ttk.Label(top, text="Duration (seconds):").grid(row=1, column=0, padx=(10, 6), pady=6, sticky="e")

        value_var = tk.StringVar(value="" if self.files[idx]["duration"] is None else f"{self.files[idx]['duration']}")
        entry = ttk.Entry(top, textvariable=value_var, width=16)
        entry.grid(row=1, column=1, padx=(0, 10), pady=6, sticky="w")
        entry.focus_set()

        def save() -> None:
            raw = value_var.get().strip()
            try:
                val = float(raw)
            except ValueError:
                messagebox.showerror("Invalid", "Duration must be a number.", parent=top)
                return
            if val <= 0:
                messagebox.showerror("Invalid", "Duration must be greater than 0.", parent=top)
                return
            self.files[idx]["duration"] = val
            self._refresh_tree()
            top.destroy()

        ttk.Button(top, text="Save", command=save).grid(row=2, column=0, padx=10, pady=(0, 10), sticky="ew")
        ttk.Button(top, text="Cancel", command=top.destroy).grid(row=2, column=1, padx=(0, 10), pady=(0, 10), sticky="ew")

    def pick_output(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="Select output file",
            defaultextension=".mp4",
            filetypes=[("MP4 Video", "*.mp4")],
        )
        if selected:
            self.output_path.set(selected)

    def _validate_inputs(self) -> list[ClipSpec]:
        if len(self.files) == 0:
            raise RuntimeError("Please add at least one video.")
        if len(self.files) > self.MAX_FILES:
            raise RuntimeError("Maximum 10 videos are allowed.")

        output = self.output_path.get().strip()
        if not output:
            raise RuntimeError("Please select an output path.")
        if not output.lower().endswith(".mp4"):
            raise RuntimeError("Output must be an .mp4 file.")

        clips: list[ClipSpec] = []
        for idx, item in enumerate(self.files, start=1):
            path = item["path"]
            duration = item["duration"]
            if duration is None:
                raise RuntimeError(f"Duration missing for file #{idx}: {Path(path).name}")
            try:
                duration = float(duration)
            except (TypeError, ValueError):
                raise RuntimeError(f"Invalid duration for file #{idx}: {Path(path).name}")
            if duration <= 0:
                raise RuntimeError(f"Duration must be > 0 for file #{idx}: {Path(path).name}")
            if not Path(path).exists():
                raise RuntimeError(f"File not found: {path}")

            metadata = probe_video(path)
            if duration > metadata.duration:
                raise RuntimeError(
                    f"Duration {duration:.2f}s exceeds source length {metadata.duration:.2f}s for {Path(path).name}"
                )
            clips.append(ClipSpec(path=path, duration=duration, metadata=metadata))
        return clips

    def start_merge(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return

        output = self.output_path.get().strip()
        if output and Path(output).exists():
            overwrite = messagebox.askyesno("Overwrite output?", f"{output}\n\nFile exists. Overwrite?")
            if not overwrite:
                return

        try:
            clips = self._validate_inputs()
        except Exception as exc:
            messagebox.showerror("Validation Error", str(exc))
            return

        self.cancel_event.clear()
        self.progress_var.set(0.0)
        self.status_var.set("Starting merge...")
        self._set_busy(True)
        started = time.time()

        def progress_cb(message: str, progress: float) -> None:
            self.ui_queue.put(("progress", message, max(0.0, min(1.0, progress))))

        def worker() -> None:
            try:
                run_merge_job(clips, output, progress_cb, self.cancel_event)
                elapsed = time.time() - started
                self.ui_queue.put(("success", output, elapsed))
            except MergeCancelled:
                self.ui_queue.put(("cancelled",))
            except Exception as exc:
                self.ui_queue.put(("error", str(exc)))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def cancel_merge(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            self.cancel_event.set()
            self.status_var.set("Cancelling...")

    def _start_queue_poll(self) -> None:
        def poll() -> None:
            while True:
                try:
                    event = self.ui_queue.get_nowait()
                except Empty:
                    break

                kind = event[0]
                if kind == "progress":
                    _, msg, prog = event
                    self.status_var.set(msg)
                    self.progress_var.set(prog)
                elif kind == "success":
                    _, output, elapsed = event
                    self._set_busy(False)
                    self.progress_var.set(1.0)
                    self.status_var.set(f"Merge completed in {elapsed:.1f}s")
                    messagebox.showinfo("Success", f"Video created:\n{output}\n\nElapsed: {elapsed:.1f}s")
                elif kind == "cancelled":
                    self._set_busy(False)
                    self.progress_var.set(0.0)
                    self.status_var.set("Merge cancelled.")
                    messagebox.showinfo("Cancelled", "Merge was cancelled.")
                elif kind == "error":
                    _, err = event
                    self._set_busy(False)
                    self.progress_var.set(0.0)
                    self.status_var.set("Merge failed.")
                    messagebox.showerror("Merge Error", err)

            self.root.after(100, poll)

        poll()


def main() -> None:
    root = tk.Tk()
    app = VideoMergerApp(root)
    root.minsize(800, 500)
    root.mainloop()


if __name__ == "__main__":
    main()
