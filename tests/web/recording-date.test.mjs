import assert from "node:assert/strict";
import test from "node:test";
import { readRecordingDate } from "../../src/asr_studio/web/audio-extract.js";

const NOW = new Date("2026-11-12T12:00:00Z");
const options = { now: NOW };

function box(type, data, extended = false) {
  const header = Buffer.alloc(extended ? 16 : 8);
  header.writeUInt32BE(extended ? 1 : header.length + data.length);
  header.write(type, 4, "latin1");
  if (extended) header.writeBigUInt64BE(BigInt(header.length + data.length), 8);
  return Buffer.concat([header, data]);
}

function movie(value, { version = 0, extended = false } = {}) {
  const header = Buffer.alloc(version === 1 ? 112 : 100);
  header[0] = version;
  const seconds = value === null ? 0 : new Date(value).getTime() / 1000 + 2_082_844_800;
  if (version === 1) header.writeBigUInt64BE(BigInt(seconds), 4);
  else header.writeUInt32BE(seconds, 4);
  return box("moov", box("mvhd", header), extended);
}

const file = (data, name = "meeting.mp4") => new File([data], name);
const localDay = (value) => {
  const date = new Date(value);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
};

test("movie creation time supports both header versions and extended boxes", async () => {
  for (const version of [0, 1]) {
    for (const extended of [false, true]) {
      for (const name of ["meeting.mp4", "meeting.MOV", "meeting.m4a"]) {
        const value = "2026-10-14T23:30:00Z";
        assert.equal(await readRecordingDate(file(movie(value, { version, extended }), name), options), localDay(value));
      }
    }
  }
});

test("missing, epoch and future dates remain empty, ignoring the file modification date", async () => {
  for (const value of [null, "1970-01-01T00:00:00Z", "2027-01-01T00:00:00Z"]) {
    assert.equal(await readRecordingDate(file(movie(value)), options), null);
  }
  assert.equal(await readRecordingDate(file(Buffer.alloc(0)), options), null);
  assert.equal(await readRecordingDate(file(movie("2026-10-14T12:00:00Z"), "meeting.wav"), options), null);
});

test("malformed sizes and truncated movie headers do not reject the file", async () => {
  const impossibleSize = box("free", Buffer.alloc(0));
  impossibleSize.writeUInt32BE(0xffff_ffff);
  for (const data of [impossibleSize, box("moov", box("mvhd", Buffer.alloc(3))), Buffer.from("invalid")]) {
    assert.equal(await readRecordingDate(file(data), options), null);
  }
});

test("a large media payload is skipped without loading it into memory", async () => {
  const value = "2026-10-14T12:00:00Z";
  const largeSize = 5 * 1024 ** 3;
  const mediaHeader = Buffer.alloc(16);
  mediaHeader.writeUInt32BE(1);
  mediaHeader.write("mdat", 4);
  mediaHeader.writeBigUInt64BE(BigInt(largeSize), 8);
  const metadata = movie(value);
  const reads = [];
  const virtualFile = {
    name: "large.mp4",
    size: largeSize + metadata.length,
    slice(start, end) {
      reads.push([start, end]);
      assert.ok(end - start <= 16, "metadata reader requested media payload");
      return new Blob([start < largeSize ? mediaHeader.subarray(start, end) : metadata.subarray(start - largeSize, end - largeSize)]);
    },
  };
  // BlobSource rejects the virtual file; header reading must still work independently.
  assert.equal(await readRecordingDate(virtualFile, options), localDay(value));
  assert.ok(reads.some(([start]) => start === largeSize));
});
