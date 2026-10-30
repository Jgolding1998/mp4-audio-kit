"use strict";
(() => {
  const token = document.querySelector('meta[name="csrf-token"]').content;
  const maxBytes = Number(document.querySelector('meta[name="max-bytes"]').content);
  const drop = document.getElementById("drop");
  const picker = document.getElementById("files");
  const list = document.getElementById("list");
  const actions = document.getElementById("actions");
  const zipBtn = document.getElementById("zip");
  const clearBtn = document.getElementById("clear");
  const noteEl = document.getElementById("note");
  const PARALLEL = 2;
  const MAX_FILES = 50;
  let entries = [];
  let running = 0;
  let clearing = false;
  let generation = 0;  // bumped by Clear so late replies cannot touch the new list

  function note(text) {
    noteEl.textContent = text || "";
    noteEl.hidden = !text;
  }

  function size(bytes) {
    if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + " KB";
    if (bytes >= 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function set(entry, state, text, kind) {
    entry.state = state;
    entry.row.className = kind || "";
    entry.stateEl.textContent = text;
    entry.act.replaceChildren();
    if (state === "done") {
      const a = document.createElement("a");
      a.href = "/api/download/" + entry.id;
      a.download = "";  // an expired link fails as a download instead of replacing this page
      a.textContent = "Download MP3";
      entry.act.appendChild(a);
    }
    refresh();
  }

  function refresh() {
    actions.hidden = entries.length === 0 && !clearing;
    const done = entries.filter((e) => e.state === "done");
    zipBtn.hidden = done.length < 2 || clearing;
    zipBtn.textContent = "Download all " + done.length + " (ZIP)";
  }

  function add(files) {
    const remaining = MAX_FILES - entries.length;
    if (files.length > remaining) note("Up to 50 files fit in one batch. Clear the list before adding more.");
    for (const file of files.slice(0, Math.max(0, remaining))) {
      const row = document.createElement("li");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = file.name;
      const stateEl = document.createElement("span");
      stateEl.className = "state";
      const act = document.createElement("span");
      act.className = "act";
      row.append(name, act, stateEl);
      list.appendChild(row);
      const entry = { file, row, stateEl, act, state: "", id: null };
      entries.push(entry);
      if (!/\.(mp4|m4v)$/i.test(file.name)) set(entry, "error", "Not an MP4 file.", "bad");
      else if (file.size > maxBytes) set(entry, "error", "Too large: " + size(file.size) + ", over the " + size(maxBytes) + " per-file limit. See --max-mb in the README.", "bad");
      else set(entry, "waiting", "Waiting...");
    }
    pump();
  }

  function pump() {
    if (clearing) return;
    while (running < PARALLEL) {
      const next = entries.find((e) => e.state === "waiting");
      if (!next) return;
      running++;
      upload(next).finally(() => { running--; pump(); });
    }
  }

  function upload(entry) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      entry.xhr = xhr;
      const form = new FormData();
      form.append("file", entry.file, entry.file.name);
      xhr.open("POST", "/api/convert");
      xhr.setRequestHeader("X-CSRF-Token", token);
      xhr.responseType = "json";
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) set(entry, "uploading", "Uploading... " + Math.round((e.loaded / e.total) * 100) + "%");
      };
      xhr.upload.onload = () => set(entry, "converting", "Converting...");
      xhr.onload = () => {
        const data = xhr.response || {};
        if (xhr.status === 200 && data.id) {
          entry.id = data.id;
          entry.row.querySelector(".name").textContent = data.name;
          set(entry, "done", "Ready, " + size(data.bytes) + ", " + Math.round(data.seconds) + " s", "ok");
        } else {
          set(entry, "error", data.message || "The conversion failed.", "bad");
        }
        resolve();
      };
      xhr.onerror = xhr.onabort = () => {
        if (entry.state !== "removed") set(entry, "error", "Lost contact with the app.", "bad");
        resolve();
      };
      set(entry, "uploading", "Uploading...");
      xhr.send(form);
    });
  }

  async function post(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
      body: JSON.stringify(body),
      credentials: "same-origin",
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    return { ok: res.ok, data };
  }

  zipBtn.addEventListener("click", async () => {
    const ids = entries.filter((e) => e.state === "done").map((e) => e.id);
    const started = generation;
    zipBtn.disabled = true;
    note("");
    zipBtn.textContent = "Preparing ZIP...";
    try {
      const { ok, data } = await post("/api/zip", { ids });
      if (started !== generation) return;  // list was cleared meanwhile
      if (ok) {
        const a = document.createElement("a");
        a.href = "/api/download/" + data.id;
        a.download = "";
        document.body.appendChild(a);
        a.click();
        a.remove();
      } else {
        note(data.message || "Could not make the ZIP.");
      }
    } catch (e) {
      if (started === generation) note("Lost contact with the app.");
    } finally {
      zipBtn.disabled = false;
      refresh();
    }
  });

  clearBtn.addEventListener("click", async () => {
    if (clearBtn.disabled) return;
    const retrying = clearing;
    clearing = true;
    clearBtn.disabled = true;
    clearBtn.textContent = "Clearing...";
    if (!retrying) {
      generation++;
      for (const e of entries) {
        e.state = "removed";
        if (e.xhr) e.xhr.abort();
      }
      entries = [];
      list.replaceChildren();
    }
    note("");
    refresh();
    try {
      const { ok, data } = await post("/api/clear", {});
      if (ok) {
        clearing = false;
      } else {
        note(data.message || "Could not clear previous files. Press Clear list to retry.");
      }
    } catch (e) {
      note("Could not clear previous files. Press Clear list to retry.");
    } finally {
      clearBtn.disabled = false;
      clearBtn.textContent = "Clear list";
      refresh();
      pump();
    }
  });

  document.getElementById("pick").addEventListener("click", () => picker.click());
  picker.addEventListener("change", () => { add(Array.from(picker.files)); picker.value = ""; });

  for (const type of ["dragenter", "dragover"]) {
    drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add("over"); });
  }
  for (const type of ["dragleave", "drop"]) {
    drop.addEventListener(type, () => drop.classList.remove("over"));
  }
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    add(Array.from(e.dataTransfer.files));
  });
  // Dropping outside the box should not make the browser open the file.
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());
})();
