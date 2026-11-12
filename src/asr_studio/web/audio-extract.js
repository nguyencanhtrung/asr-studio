// Cuts the audio track out of a video file in the browser by copying its packets (no decode, no re-encode),
// so a long meeting video reaches the server as a small audio file. Also reads capture dates locally.
// Loaded on the first file that needs metadata or audio extraction.
// Any problem throws an Error with a `kind` ("unsupported" | "no-audio" | "codec" | "failed"); the caller then
// uploads the original file. The vendored library is unmodified: see vendor/mediabunny/PROVENANCE.md.
import {
  ALL_FORMATS,
  BlobSource,
  BufferTarget,
  Conversion,
  Input,
  Mp4OutputFormat,
  Output,
  WebMOutputFormat,
} from "./vendor/mediabunny/mediabunny.min.mjs";

const fail = (kind, message) => Object.assign(new Error(message), { kind });

// container of the source -> the audio codec we can copy and the file we write it into.
const TARGETS = {
  ".mp4": { codec: "aac", format: () => new Mp4OutputFormat(), extension: ".m4a", type: "audio/mp4" },
  ".mov": { codec: "aac", format: () => new Mp4OutputFormat(), extension: ".m4a", type: "audio/mp4" },
  ".webm": { codec: "opus", format: () => new WebMOutputFormat(), extension: ".webm", type: "audio/webm" },
};

// Skip media payloads by box length. Even a multi-GB mdat only costs a 16-byte read.
async function findBox(file, start, end, type, budget) {
  while (start + 8 <= end && budget.remaining-- > 0) {
    const bytes = new DataView(await file.slice(start, Math.min(start + 16, end)).arrayBuffer());
    if (bytes.byteLength < 8) return null;
    const name = String.fromCharCode(...new Uint8Array(bytes.buffer, 4, 4));
    let size = bytes.getUint32(0);
    let header = 8;
    if (size === 1) {
      if (bytes.byteLength < 16) return null;
      size = Number(bytes.getBigUint64(8));
      header = 16;
    } else if (size === 0) size = end - start;
    if (!Number.isSafeInteger(size) || size < header || size > end - start) return null;
    if (name === type) return { start: start + header, end: start + size };
    start += size;
  }
  return null;
}

async function movieCreationDate(file) {
  const budget = { remaining: 1024 };
  const movie = await findBox(file, 0, file.size, "moov", budget);
  if (!movie) return null;
  const header = await findBox(file, movie.start, movie.end, "mvhd", budget);
  if (!header) return null;
  const bytes = new DataView(await file.slice(header.start, Math.min(header.start + 12, header.end)).arrayBuffer());
  if (bytes.byteLength < 8) return null;
  const version = bytes.getUint8(0);
  if (version !== 0 && version !== 1) return null;
  if (version === 1 && bytes.byteLength < 12) return null;
  const seconds = version === 1 ? Number(bytes.getBigUint64(4)) : bytes.getUint32(4);
  if (!Number.isSafeInteger(seconds) || seconds === 0) return null;
  return new Date((seconds - 2_082_844_800) * 1000); // QuickTime epoch: 1904-01-01 UTC.
}

function plausibleDate(date, now) {
  const timestamp = date?.getTime();
  return Number.isFinite(timestamp) && timestamp > 0 && timestamp <= now.getTime() + 86_400_000;
}

async function recordingDateFromFile(file, now) {
  const extension = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (![".mp4", ".mov", ".m4a", ".webm"].includes(extension)) return null;
  // Prefer an explicit capture tag: its offset preserves the day where the recording was made.
  let input;
  try {
    input = new Input({ source: new BlobSource(file), formats: ALL_FORMATS });
    const tags = await input.getMetadataTags();
    const value = tags.raw?.["com.apple.quicktime.creationdate"]
      ?? Object.entries(tags.raw || {}).find(([key]) => key.toUpperCase() === "DATE_RECORDED")?.[1];
    if (typeof value === "string") {
      const match = /^(\d{4}-\d{2}-\d{2})(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))?$/.exec(value);
      const date = new Date(value.length === 10 ? `${value}T00:00:00Z` : value);
      if (match && plausibleDate(date, now)) {
        const calendar = new Date(`${match[1]}T00:00:00Z`);
        if (calendar.toISOString().slice(0, 10) === match[1]) return match[1];
      }
    }
  } catch {
    // Some valid files have metadata the library cannot parse; try the movie header.
  } finally {
    input?.dispose();
  }
  if (extension === ".webm") return null;
  const date = await movieCreationDate(file);
  if (!plausibleDate(date, now)) return null;
  // A movie header has UTC but no recording-zone offset. Use the browser's local day.
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

export async function readRecordingDate(file, { now = new Date() } = {}) {
  let timer;
  try {
    return await Promise.race([
      recordingDateFromFile(file, now),
      new Promise((resolve) => { timer = setTimeout(() => resolve(null), 3000); }),
    ]);
  } catch {
    return null; // Missing or malformed metadata must never prevent an upload.
  } finally {
    clearTimeout(timer);
  }
}

export async function extractAudio(file, { signal, onProgress } = {}) {
  const dot = file.name.lastIndexOf(".");
  const target = TARGETS[dot >= 0 ? file.name.slice(dot).toLowerCase() : ""];
  if (!target) throw fail("unsupported", "not a video container");
  signal?.throwIfAborted();
  const input = new Input({ source: new BlobSource(file), formats: ALL_FORMATS });
  let conversion = null;
  const onAbort = () => conversion?.cancel();
  signal?.addEventListener("abort", onAbort);
  try {
    const track = await input.getPrimaryAudioTrack();
    if (!track) throw fail("no-audio", "no audio track");
    if (track.codec !== target.codec) throw fail("codec", `audio codec ${track.codec} cannot be copied`);
    const bufferTarget = new BufferTarget();
    const output = new Output({ format: target.format(), target: bufferTarget });
    conversion = await Conversion.init({
      input,
      output,
      video: { discard: true },
      audio: { forceTranscode: false },
      copy: { mode: "forced" }, // never fall back to a transcode
      showWarnings: false,
    });
    if (!conversion.isValid || conversion.discardedTracks.some((entry) => entry.track === track)) {
      throw fail("codec", "audio track cannot be copied");
    }
    conversion.onProgress = (ratio) => onProgress?.(ratio);
    signal?.throwIfAborted();
    await conversion.execute();
    signal?.throwIfAborted();
    const stem = dot >= 0 ? file.name.slice(0, dot) : file.name;
    return new File([bufferTarget.buffer], `${stem}${target.extension}`, { type: target.type });
  } catch (error) {
    if (signal?.aborted) throw signal.reason ?? error;
    throw error.kind ? error : fail("failed", String(error?.message ?? error));
  } finally {
    signal?.removeEventListener("abort", onAbort);
    input.dispose();
  }
}
