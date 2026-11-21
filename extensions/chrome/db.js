// A new database name prevents an installed prototype from adopting its
// credentials, sessions, or audio chunks during the production update.
const DB_NAME = "asr-extension-production-v1";
const DB_VERSION = 1;
const SESSION_STORE = "sessions";
const CHUNK_STORE = "chunks";
const chunkKey = (sessionId, sequence) => `${sessionId}:${sequence}`;

let databasePromise;

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("IndexedDB request failed."));
  });
}

function openDatabase() {
  if (databasePromise) return databasePromise;
  databasePromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(SESSION_STORE)) {
        database.createObjectStore(SESSION_STORE, { keyPath: "clientRecordingId" });
      }
      if (!database.objectStoreNames.contains(CHUNK_STORE)) {
        const chunks = database.createObjectStore(CHUNK_STORE, { keyPath: "key" });
        chunks.createIndex("bySessionSequence", ["sessionId", "sequence"], { unique: true });
      }
    };
    request.onsuccess = () => {
      const database = request.result;
      database.onversionchange = () => database.close();
      resolve(database);
    };
    request.onerror = () => reject(request.error || new Error("IndexedDB could not open."));
  });
  return databasePromise;
}

function makeTransaction(database, stores, mode) {
  try {
    return database.transaction(stores, mode, { durability: "strict" });
  } catch {
    return database.transaction(stores, mode);
  }
}

function runTransaction(stores, mode, callback) {
  return openDatabase().then((database) => new Promise((resolve, reject) => {
    let transaction;
    try {
      transaction = makeTransaction(database, stores, mode);
    } catch (error) {
      reject(error);
      return;
    }

    let result;
    let callbackDone = false;
    let callbackError = null;
    transaction.oncomplete = () => {
      if (callbackError) reject(callbackError);
      else resolve(result);
    };
    transaction.onerror = () => reject(transaction.error || callbackError || new Error("IndexedDB transaction failed."));
    transaction.onabort = () => reject(transaction.error || callbackError || new Error("IndexedDB transaction aborted."));

    Promise.resolve()
      .then(() => callback(Object.fromEntries(stores.map((name) => [name, transaction.objectStore(name)]))))
      .then((value) => {
        result = value;
        callbackDone = true;
      })
      .catch((error) => {
        callbackError = error;
        try {
          transaction.abort();
        } catch {
          // The transaction may already be complete.
        }
        if (callbackDone) reject(error);
      });
  }));
}

function mergeSession(current, section, patch) {
  const next = { ...current };
  if (section) next[section] = { ...(current[section] || {}), ...patch };
  else Object.assign(next, patch);
  next.revision = (current.revision || 0) + 1;
  next.updatedAt = Date.now();
  return next;
}

export async function createCaptureSession({ clientRecordingId, endpoint, title, mimeType, captureEpoch, metadata, limits }) {
  const initial = {
    clientRecordingId,
    endpoint: { ...endpoint },
    title,
    mimeType,
    createdAt: Date.now(),
    revision: 1,
    capture: {
      status: "starting",
      captureEpoch,
      captureComplete: false,
      interrupted: false,
      persistenceError: "",
      nextSequence: 0,
      chunkCount: 0,
      totalBytes: 0,
      maxChunkBytes: limits.max_chunk_bytes,
      maxRecordingBytes: limits.max_recording_bytes,
      maxLocalBytes: limits.max_local_bytes,
      maxChunks: limits.max_chunks,
      metadata: { ...metadata },
      createPayload: null,
      gates: { tab: true, microphone: true },
      tabMonitor: true,
      levels: { tab: 0, microphone: 0 }
    },
    upload: {
      state: "waiting_for_stop",
      createAttempted: false,
      serverSessionId: null,
      serverEpoch: null,
      nextSequence: 0,
      nextOffset: 0,
      retryAt: null,
      cancelRetryAt: null,
      cancelError: null,
      cancelSettled: false,
      finalizeId: null,
      finalizePayload: null,
      finalizeSubmitted: false,
      manifestHash: null,
      receipt: null,
      meetingUrl: null,
      cleaned: false,
      error: null
    },
    summary: null
  };

  return runTransaction([SESSION_STORE], "readwrite", async ({ sessions }) => {
    const existing = await requestResult(sessions.get(clientRecordingId));
    if (existing) return { created: false, session: existing };
    await requestResult(sessions.add(initial));
    return { created: true, session: initial };
  });
}

export async function getSession(clientRecordingId) {
  return runTransaction([SESSION_STORE], "readonly", ({ sessions }) => requestResult(sessions.get(clientRecordingId)));
}

export async function listSessions() {
  return runTransaction([SESSION_STORE], "readonly", ({ sessions }) => requestResult(sessions.getAll()));
}

export async function patchSession(clientRecordingId, section, patch) {
  return runTransaction([SESSION_STORE], "readwrite", async ({ sessions }) => {
    const current = await requestResult(sessions.get(clientRecordingId));
    if (!current) throw new Error(`Unknown capture session ${clientRecordingId}.`);
    const next = mergeSession(current, section, patch);
    await requestResult(sessions.put(next));
    return next;
  });
}

// Reads and writes in one transaction. `build` returns the patch for the current
// row, or null to leave it unchanged; `applied` tells which happened.
export async function patchSessionWith(clientRecordingId, section, build) {
  return runTransaction([SESSION_STORE], "readwrite", async ({ sessions }) => {
    const current = await requestResult(sessions.get(clientRecordingId));
    if (!current) throw new Error(`Unknown capture session ${clientRecordingId}.`);
    const patch = build(current);
    if (!patch) return { applied: false, session: current };
    const next = mergeSession(current, section, patch);
    await requestResult(sessions.put(next));
    return { applied: true, session: next };
  });
}

// Commits the first recovery choice in one transaction. `decide` is the pure
// validator; a second, different choice reads the committed one and is refused.
export async function commitRecoveryChoice(clientRecordingId, choice, decide) {
  return runTransaction([SESSION_STORE], "readwrite", async ({ sessions }) => {
    const current = await requestResult(sessions.get(clientRecordingId));
    if (!current) return { ok: false, code: "capture_missing", session: null };
    const decision = decide(current, choice);
    if (!decision.ok) return { ok: false, code: decision.code, session: current };
    const next = mergeSession(current, "upload", {
      recoveryChoice: choice,
      retryAt: null,
      cancelRetryAt: null,
      cancelError: null,
      error: null
    });
    await requestResult(sessions.put(next));
    return { ok: true, session: next };
  });
}

export async function appendCaptureChunk(clientRecordingId, chunk) {
  return runTransaction([SESSION_STORE, CHUNK_STORE], "readwrite", async ({ sessions, chunks }) => {
    const current = await requestResult(sessions.get(clientRecordingId));
    if (!current) throw new Error("The local capture session does not exist.");
    const capture = current.capture;
    if (!["starting", "recording", "stopping"].includes(capture.status)) {
      throw new Error("The capture is not accepting audio chunks.");
    }
    if (chunk.sequence !== capture.nextSequence || chunk.offset !== capture.totalBytes) {
      throw new Error("The local capture sequence is not contiguous.");
    }
    if (chunk.length <= 0 || chunk.length > capture.maxChunkBytes || chunk.blob.size !== chunk.length) {
      throw new Error("The local audio chunk exceeds the configured limit.");
    }
    if (capture.chunkCount + 1 > capture.maxChunks || capture.totalBytes + chunk.length > capture.maxRecordingBytes || capture.totalBytes + chunk.length > capture.maxLocalBytes) {
      const error = new Error("The local audio cap was reached.");
      error.code = "local_quota";
      throw error;
    }

    const key = chunkKey(clientRecordingId, chunk.sequence);
    const prior = await requestResult(chunks.get(key));
    if (prior) {
      if (prior.sha256 !== chunk.sha256 || prior.length !== chunk.length || prior.offset !== chunk.offset) {
        throw new Error("A duplicate local chunk has different metadata.");
      }
      return current;
    }

    await requestResult(chunks.add({
      key,
      sessionId: clientRecordingId,
      sequence: chunk.sequence,
      offset: chunk.offset,
      length: chunk.length,
      sha256: chunk.sha256,
      blob: chunk.blob
    }));
    const next = mergeSession(current, "capture", {
      nextSequence: capture.nextSequence + 1,
      chunkCount: capture.chunkCount + 1,
      totalBytes: capture.totalBytes + chunk.length
    });
    await requestResult(sessions.put(next));
    return next;
  });
}

export async function getChunk(clientRecordingId, sequence) {
  return runTransaction([CHUNK_STORE], "readonly", ({ chunks }) => requestResult(chunks.get(chunkKey(clientRecordingId, sequence))));
}

export async function listChunkMetadata(clientRecordingId) {
  return runTransaction([CHUNK_STORE], "readonly", ({ chunks }) => new Promise((resolve, reject) => {
    const index = chunks.index("bySessionSequence");
    const range = IDBKeyRange.bound([clientRecordingId, 0], [clientRecordingId, Number.MAX_SAFE_INTEGER]);
    const result = [];
    const request = index.openCursor(range);
    request.onerror = () => reject(request.error || new Error("IndexedDB cursor failed."));
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        resolve(result);
        return;
      }
      const value = cursor.value;
      result.push({ sequence: value.sequence, offset: value.offset, length: value.length, sha256: value.sha256 });
      cursor.continue();
    };
  }));
}

export async function deleteChunks(clientRecordingId) {
  return runTransaction([CHUNK_STORE], "readwrite", ({ chunks }) => new Promise((resolve, reject) => {
    const index = chunks.index("bySessionSequence");
    const range = IDBKeyRange.bound([clientRecordingId, 0], [clientRecordingId, Number.MAX_SAFE_INTEGER]);
    const request = index.openCursor(range);
    request.onerror = () => reject(request.error || new Error("IndexedDB cursor failed."));
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        resolve(true);
        return;
      }
      cursor.delete();
      cursor.continue();
    };
  }));
}

export async function cleanupReceipt(clientRecordingId, uploadPatch, summary) {
  return runTransaction([SESSION_STORE, CHUNK_STORE], "readwrite", async ({ sessions, chunks }) => {
    const current = await requestResult(sessions.get(clientRecordingId));
    if (!current) throw new Error("The local capture session does not exist.");
    const next = mergeSession(current, "upload", uploadPatch);
    next.summary = { ...(current.summary || {}), ...summary };
    await new Promise((resolve, reject) => {
      const index = chunks.index("bySessionSequence");
      const range = IDBKeyRange.bound([clientRecordingId, 0], [clientRecordingId, Number.MAX_SAFE_INTEGER]);
      const request = index.openCursor(range);
      request.onerror = () => reject(request.error || new Error("IndexedDB cursor failed."));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) {
          resolve(true);
          return;
        }
        cursor.delete();
        cursor.continue();
      };
    });
    await requestResult(sessions.put(next));
    return next;
  });
}

export async function discardLocalCapture(clientRecordingId) {
  return runTransaction([SESSION_STORE, CHUNK_STORE], "readwrite", async ({ sessions, chunks }) => {
    const current = await requestResult(sessions.get(clientRecordingId));
    if (!current) throw new Error("The local capture session does not exist.");
    if (["starting", "recording", "stopping"].includes(current.capture.status)) {
      throw new Error("Stop the active capture before discarding local audio.");
    }
    if (current.upload.receipt) throw new Error("A receipt already exists. Keep the retained summary.");
    await new Promise((resolve, reject) => {
      const index = chunks.index("bySessionSequence");
      const range = IDBKeyRange.bound([clientRecordingId, 0], [clientRecordingId, Number.MAX_SAFE_INTEGER]);
      const request = index.openCursor(range);
      request.onerror = () => reject(request.error || new Error("IndexedDB cursor failed."));
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) {
          resolve(true);
          return;
        }
        cursor.delete();
        cursor.continue();
      };
    });
    await requestResult(sessions.delete(clientRecordingId));
    return true;
  });
}

export async function countChunks(clientRecordingId) {
  return runTransaction([CHUNK_STORE], "readonly", ({ chunks }) => {
    const range = IDBKeyRange.bound([clientRecordingId, 0], [clientRecordingId, Number.MAX_SAFE_INTEGER]);
    return requestResult(chunks.index("bySessionSequence").count(range));
  });
}

export function hasPendingLocalAudio(session) {
  return Boolean(session && session.capture && session.capture.chunkCount > 0 && !session.upload.cleaned);
}

export function isActiveCapture(session) {
  return Boolean(session && session.capture && ["starting", "recording", "stopping"].includes(session.capture.status));
}

export function isUploadable(session) {
  return Boolean(session && session.capture?.createPayload && !session.capture?.interrupted && !session.upload?.cleaned);
}

export async function getActiveCapture() {
  const sessions = await listSessions();
  return sessions.find(isActiveCapture) || null;
}

// Earlier clients retained discarded summaries. Remove only records that were
// explicitly discarded; interrupted and unconfirmed recordings remain recoverable.
export async function purgeDiscardedCaptures() {
  for (const session of await listSessions()) {
    if (session.capture?.status === "discarded" && session.upload?.cleaned && !session.upload.receipt) {
      await discardLocalCapture(session.clientRecordingId);
    }
  }
}
